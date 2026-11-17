"""Starts agent runs for changes and tracks them to completion.

`begin_agent_run` (in the change store) is what reserves a change, so a second request, a duplicate
alert or a retry can never start a second run for the same change. Runs execute on a small thread
pool so intake and the API never wait for a model or a dev deploy.

With a durable checkpointer a run that a restart interrupted is resumed from its last completed stage
(`recover`). The number of resumes is capped, and what the run had already used of its budget is carried
over, so a process that keeps dying cannot keep a run going for ever.
"""
import dataclasses
import logging
from concurrent.futures import Future, ThreadPoolExecutor

from langgraph.checkpoint.memory import MemorySaver

from app.agents.graph import AgentRuntime, build_incident_graph, branch_for
from app.agents.models import IncidentContext
from app.agents.shadow import ShadowRecorder
from app.models.schemas import ChangeRecord

logger = logging.getLogger("app.agent_runner")


def context_for(record: ChangeRecord) -> IncidentContext:
    alert = record.alert or {}
    return IncidentContext(
        incident_id=record.incident_id,
        service=alert["service"],
        environment=alert.get("environment", "prod"),
        alert_metric=alert["metric"],
        alert_message=alert.get("message", ""),
        value=float(alert["value"]),
        threshold=float(alert["threshold"]),
        occurred_at=alert["timestamp"],
    )


class AgentRunner:
    def __init__(self, runtime: AgentRuntime, workers: int = 2, checkpointer=None, max_resumes: int = 2) -> None:
        self.runtime = runtime
        self.checkpointer = checkpointer or MemorySaver()
        self.max_resumes = max(0, max_resumes)
        self.graph = build_incident_graph(runtime, self.checkpointer)
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="agent-run")

    # ------------------------------------------------------------------ resuming after a restart

    def resume(self, change_id: str, actor: str = "system") -> tuple[ChangeRecord, Future]:
        """Continue an interrupted run in the background. Raises ValueError if it cannot or may not be resumed."""
        record = self._prepare_resume(change_id, actor)
        return record, self._pool.submit(self._execute, record, resume=True)

    def resume_sync(self, change_id: str, actor: str = "system") -> ChangeRecord:
        return self._execute(self._prepare_resume(change_id, actor), resume=True)

    def recover(self, actor: str = "system") -> dict[str, list[str]]:
        """At startup: resume the runs a restart interrupted, and escalate the ones that cannot be resumed."""
        store = self.runtime.change_store
        resumed: list[str] = []
        escalated: list[str] = []
        for record in store.list_changes():
            if record.agent_run_status != "running":
                continue
            try:
                self.resume(record.change_id, actor)
                resumed.append(record.change_id)
            except ValueError as exc:
                try:
                    store.record_agent_step(
                        record.change_id, actor, "escalated", "agent.interrupted",
                        escalation_reason=f"the agent run was interrupted by a restart and was not resumed ({exc}); run it again",
                    )
                    escalated.append(record.change_id)
                except ValueError:
                    continue
        # Shadow runs are never resumed; this closes them (and anything else still marked running).
        escalated.extend(store.recover_interrupted_runs(actor, skip=set(resumed)))
        return {"resumed": resumed, "escalated": sorted(set(escalated))}

    def _prepare_resume(self, change_id: str, actor: str) -> ChangeRecord:
        store = self.runtime.change_store
        record = store.get_change(change_id)
        if record is None:
            raise ValueError("Change not found")
        if record.agent_run_status != "running":
            raise ValueError("No agent run is in progress for this change")
        saved = self.graph.get_state(self._config(record))
        if not saved.next:
            raise ValueError("no saved progress")
        record = store.note_agent_resume(change_id, actor, self.max_resumes)
        self.runtime.mark_resumed(change_id, saved.values.get("budget_used"))
        return record

    @staticmethod
    def _config(record: ChangeRecord, thread_tag: str = "run") -> dict:
        number = (record.shadow if thread_tag == "shadow" else record.agent_run).get("run", 1)
        return {"configurable": {"thread_id": f"{record.change_id}:{thread_tag}:{number}"}}

    def start(self, change_id: str, actor: str) -> tuple[ChangeRecord, Future]:
        """Reserve the change and run the agents in the background."""
        record = self.runtime.change_store.begin_agent_run(change_id, actor)
        return record, self._pool.submit(self._execute, record)

    def start_shadow(self, change_id: str, actor: str) -> tuple[ChangeRecord, Future]:
        """Run the agents in observe-only mode: nothing they produce enters the approval flow."""
        record = self.runtime.change_store.begin_shadow_run(change_id, actor)
        return record, self._pool.submit(self._execute_shadow, record)

    def run_shadow_sync(self, change_id: str, actor: str) -> ChangeRecord:
        record = self.runtime.change_store.begin_shadow_run(change_id, actor)
        return self._execute_shadow(record)

    def run_sync(self, change_id: str, actor: str) -> ChangeRecord:
        """Reserve the change and run the agents to completion in this thread (tests, scripts)."""
        record = self.runtime.change_store.begin_agent_run(change_id, actor)
        return self._execute(record)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _execute_shadow(self, record: ChangeRecord) -> ChangeRecord:
        # A runtime of its own per run: it writes to the shadow record, and its worktree, branch and budget
        # are separate from any real run on the same change.
        runtime = dataclasses.replace(
            self.runtime, change_store=ShadowRecorder(self.runtime.change_store), _sandboxes={}, _budgets={}
        )
        graph = build_incident_graph(runtime)
        return self._execute(record, runtime=runtime, graph=graph, namespace="agent-shadow", thread_tag="shadow")

    def _execute(self, record: ChangeRecord, runtime: AgentRuntime | None = None, graph=None,
                 namespace: str = "agent-fix", thread_tag: str = "run", resume: bool = False) -> ChangeRecord:
        runtime = runtime or self.runtime
        graph = graph or self.graph
        change_id = record.change_id
        store = runtime.change_store
        config = self._config(record, thread_tag)
        try:
            if resume:
                graph.invoke(None, config=config)  # None: continue from the last saved stage
            else:
                ctx = context_for(record)
                state = {
                    "change_id": change_id,
                    "ctx": ctx.model_dump(mode="json"),
                    "branch": branch_for(change_id, ctx.service, namespace),
                    "attempt": 1,
                    "attempts": [],
                }
                graph.invoke(state, config=config)
        except Exception as exc:
            logger.exception("agent-run-failed", extra={"change_id": change_id})
            try:
                store.record_agent_step(
                    change_id, "agent:orchestrator", "escalated", "agent.failed", {},
                    escalation_reason=f"the agent run failed unexpectedly ({type(exc).__name__}: {exc})"[:300],
                )
            except ValueError:
                pass
        finally:
            runtime.release(change_id)
            self._forget_finished(change_id, config, graph)
        return self.runtime.change_store.get_change(change_id) or record

    def _forget_finished(self, change_id: str, config: dict, graph) -> None:
        """Drop a finished run's saved progress. A run that is still `running` here was interrupted: keep it."""
        if graph is not self.graph:
            return  # a shadow run keeps its progress in memory only
        current = self.runtime.change_store.get_change(change_id)
        if current is not None and current.agent_run_status == "running":
            return
        try:
            self.checkpointer.delete_thread(config["configurable"]["thread_id"])
        except Exception:
            logger.exception("checkpoint-cleanup-failed", extra={"change_id": change_id})
