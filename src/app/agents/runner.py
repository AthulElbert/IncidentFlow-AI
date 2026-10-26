"""Starts agent runs for changes and tracks them to completion.

`begin_agent_run` (in the change store) is what reserves a change, so a second request, a duplicate
alert or a retry can never start a second run for the same change. Runs execute on a small thread
pool so intake and the API never wait for a model or a dev deploy.
"""
import logging
from concurrent.futures import Future, ThreadPoolExecutor

from app.agents.graph import AgentRuntime, build_incident_graph, branch_for
from app.agents.models import IncidentContext
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
    def __init__(self, runtime: AgentRuntime, workers: int = 2, checkpointer=None) -> None:
        self.runtime = runtime
        self.graph = build_incident_graph(runtime, checkpointer)
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="agent-run")

    def start(self, change_id: str, actor: str) -> tuple[ChangeRecord, Future]:
        """Reserve the change and run the agents in the background."""
        record = self.runtime.change_store.begin_agent_run(change_id, actor)
        return record, self._pool.submit(self._execute, record)

    def run_sync(self, change_id: str, actor: str) -> ChangeRecord:
        """Reserve the change and run the agents to completion in this thread (tests, scripts)."""
        record = self.runtime.change_store.begin_agent_run(change_id, actor)
        return self._execute(record)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _execute(self, record: ChangeRecord) -> ChangeRecord:
        change_id = record.change_id
        store = self.runtime.change_store
        try:
            ctx = context_for(record)
            state = {
                "change_id": change_id,
                "ctx": ctx.model_dump(mode="json"),
                "branch": branch_for(change_id, ctx.service),
                "attempt": 1,
                "attempts": [],
            }
            thread = f"{change_id}:{record.agent_run.get('run', 1)}"
            self.graph.invoke(state, config={"configurable": {"thread_id": thread}})
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
            self.runtime.release(change_id)
        return store.get_change(change_id) or record
