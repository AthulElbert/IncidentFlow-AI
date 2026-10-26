"""The incident run: Investigator -> Fixer -> Verifier -> Critic, as a LangGraph graph.

The graph decides what happens next. It does not decide what is allowed: every state change goes
through ChangeControlStore methods, which enforce the lifecycle table and write the audit trail. The
run can reach `ready_for_prod` (awaiting a person) or `escalated` and nothing further; approving and
promoting are not reachable from here.

    investigate --proceed--> fix --proposed--> verify --passed--> review --approve--> package -> END
        |                     |                  |  \\                |  \\
        escalate              escalate           |   failed, retries left / send_back, retries left
                                                 |        \\_______ retry (reset worktree) -> fix
                                                 inconclusive or out of retries / block -> escalate

Any unexpected exception in a node ends the run in `escalated` rather than leaving the change stuck.
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.agents.budget import BudgetLimits, RunBudget
from app.agents.critic import Critic
from app.agents.fixer import Fixer
from app.agents.investigator import Investigator
from app.agents.models import FixProposal, IncidentContext, InvestigationReport, VerificationReport
from app.agents.verifier import Verifier
from app.audit import AuditLog
from app.services.change_control import ChangeControlStore
from app.tools.instrumentation import ChainedBudget, ToolBudget, Toolkit
from app.tools.sandbox import FileSandbox

logger = logging.getLogger("app.agent_graph")

ORCHESTRATOR = "agent:orchestrator"
VERIFIER_ACTOR = "agent:verifier"
MAX_STORED_DIFF_CHARS = 40_000


def stored_diff(text: str) -> dict:
    """The diff a person will be asked to approve, kept on the change. The hash covers the whole diff, even if the text is cut."""
    return {
        "text": text[:MAX_STORED_DIFF_CHARS],
        "truncated": len(text) > MAX_STORED_DIFF_CHARS,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


class SandboxProvider(Protocol):
    def open(self, branch: str) -> FileSandbox:
        ...


class RunState(TypedDict, total=False):
    change_id: str
    ctx: dict
    branch: str
    attempt: int
    feedback: str
    attempts: list
    investigation: dict
    proposal: dict
    verification: dict
    review: dict
    next_step: str
    outcome: str
    crashed: str


def branch_for(change_id: str, service: str, namespace: str = "agent-fix") -> str:
    """The branch an agent run works on.

    Deliberately not `agent/...`, which the prepare-pr flow uses for the same change: recreating that
    branch would reset it and discard commits the PR flow made there.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", service.strip().lower()).strip("-") or "service"
    return f"{namespace}/{change_id.lower()}-{slug}"


@dataclass
class AgentRuntime:
    change_store: ChangeControlStore
    toolkit: Toolkit
    sandbox_provider: SandboxProvider
    investigator: Investigator
    fixer: Fixer
    verifier: Verifier
    critic: Critic
    audit: AuditLog | None = None
    max_attempts: int = 3
    budget_limits: BudgetLimits = field(default_factory=BudgetLimits)
    _sandboxes: dict[str, FileSandbox] = field(default_factory=dict)
    _budgets: dict[str, RunBudget] = field(default_factory=dict)

    def budget_for(self, change_id: str) -> RunBudget:
        if change_id not in self._budgets:
            self._budgets[change_id] = RunBudget(self.budget_limits)
        return self._budgets[change_id]

    def sandbox_for(self, change_id: str, branch: str) -> FileSandbox:
        if change_id not in self._sandboxes:
            self._sandboxes[change_id] = self.sandbox_provider.open(branch)
        return self._sandboxes[change_id]

    def release(self, change_id: str) -> None:
        self._sandboxes.pop(change_id, None)
        self._budgets.pop(change_id, None)


def _short_checks(report: dict) -> list[dict]:
    return [{"name": c["name"], "status": c["status"], "detail": c["detail"]} for c in report.get("checks", [])]


def build_incident_graph(runtime: AgentRuntime, checkpointer: Any | None = None):
    store = runtime.change_store

    def tools_for(agent: str, change_id: str, sandbox: FileSandbox | None = None):
        """An agent's tools, counted against its own cap and against the run's total."""
        budget = ChainedBudget(ToolBudget(runtime.toolkit.max_calls_per_run), runtime.budget_for(change_id).tool_calls)
        return runtime.toolkit.for_agent(agent, entity_id=change_id, audit=runtime.audit, budget=budget, sandbox=sandbox)

    def guarded(name: str, node: Callable[[RunState], dict]) -> Callable[[RunState], dict]:
        def run(state: RunState) -> dict:
            try:
                # A finished, reviewed fix is not thrown away because the clock ran out while packaging it.
                limit = None if name == "package" else runtime.budget_for(state["change_id"]).exceeded()
                if limit:
                    return escalate(state, ORCHESTRATOR, f"run budget exceeded: {limit}")
                return node(state)
            except Exception as exc:  # never leave the change stuck in a working state
                logger.exception("agent-node-crashed", extra={"node": name, "change_id": state.get("change_id")})
                return {"crashed": f"{name}: {type(exc).__name__}: {exc}"[:300]}

        return run

    def escalate(state: RunState, actor: str, reason: str, run_patch: dict | None = None, **fields) -> dict:
        patch = {**(run_patch or {}), "budget": runtime.budget_for(state["change_id"]).usage()}
        store.record_agent_step(state["change_id"], actor, "escalated", "agent.escalated", patch, escalation_reason=reason, **fields)
        return {"outcome": "escalated", "next_step": "end"}

    # ------------------------------------------------------------------- nodes

    def investigate(state: RunState) -> dict:
        change_id = state["change_id"]
        ctx = IncidentContext(**state["ctx"])
        tools = tools_for("investigator", change_id)
        report = runtime.investigator.investigate(ctx, tools)
        dump = report.model_dump(mode="json")
        top = report.top
        fields = {"issue_type": top.issue_type, "confidence": top.confidence} if top else {}
        if report.decision == "proceed":
            store.record_agent_step(
                change_id, "agent:investigator", "hypothesis_ready", "agent.investigated", {"investigation": dump},
                note=f"{top.issue_type}, confidence {top.confidence:.2f}" if top else "", **fields,
            )
            return {"investigation": dump, "next_step": "fix"}
        return {
            "investigation": dump,
            **escalate(state, "agent:investigator", report.escalation_reason or "the investigation did not reach a diagnosis",
                       {"investigation": dump}, **fields),
        }

    def fix(state: RunState) -> dict:
        change_id, attempt = state["change_id"], state["attempt"]
        ctx = IncidentContext(**state["ctx"])
        investigation = InvestigationReport(**state["investigation"])
        # A worktree (a real git branch) is only created when the Fixer can use one.
        sandbox = runtime.sandbox_for(change_id, state["branch"]) if runtime.fixer.llm is not None else None
        if sandbox is not None and attempt > 1:
            sandbox.reset()  # every attempt starts from the original code, with the feedback in hand

        store.record_agent_step(
            change_id, ORCHESTRATOR, "fixing", "agent.fixing", {"attempts": state.get("attempts", [])}, note=f"attempt {attempt}"
        )
        tools = tools_for("fixer", change_id, sandbox)
        result = runtime.fixer.propose(
            ctx, investigation.hypotheses[0], investigation.evidence, tools,
            branch=state["branch"], feedback=state.get("feedback", ""), attempt=attempt,
            llm_gate=runtime.budget_for(change_id).spend_llm,
        )
        entry = {
            "attempt": attempt,
            "fix": {
                "status": result.status,
                "summary": result.summary,
                "reason": result.reason,
                "files": result.proposal.claimed_files if result.proposal else [],
                "steps": result.steps,
            },
        }
        attempts = [*state.get("attempts", []), entry]
        if result.status != "proposed" or result.proposal is None:
            return {
                "attempts": attempts,
                **escalate(state, "agent:fixer", f"the Fixer proposed no change: {result.reason or result.status}", {"attempts": attempts}),
            }
        return {"attempts": attempts, "proposal": result.proposal.model_dump(mode="json"), "next_step": "verify"}

    def verify(state: RunState) -> dict:
        change_id, attempt = state["change_id"], state["attempt"]
        ctx = IncidentContext(**state["ctx"])
        investigation = InvestigationReport(**state["investigation"])
        sandbox = runtime.sandbox_for(change_id, state["branch"])

        store.record_agent_step(change_id, ORCHESTRATOR, "verifying", "agent.verifying", {"attempts": state["attempts"]})
        tools = tools_for("verifier", change_id, sandbox)
        report = runtime.verifier.verify(
            ctx, FixProposal(**state["proposal"]), tools, issue_type=investigation.hypotheses[0].issue_type
        )
        dump = report.model_dump(mode="json")
        attempts = [dict(item) for item in state["attempts"]]
        attempts[-1]["verification"] = {
            "verdict": report.verdict,
            "checks": _short_checks(dump),
            "alert_metric": report.alert_metric,
        }
        patch = {"verification": dump, "attempts": attempts}
        store.record_agent_step(
            change_id, "agent:verifier", "verifying", "agent.verified", patch, note=f"attempt {attempt}: {report.verdict}"
        )

        if report.verdict == "passed":
            return {"verification": dump, "attempts": attempts, "next_step": "review"}
        if report.verdict == "failed" and attempt < runtime.max_attempts:
            return {"verification": dump, "attempts": attempts, "feedback": report.feedback_for_fixer, "next_step": "retry"}
        if report.verdict == "failed":
            reason = f"verification failed on all {attempt} attempt(s):\n{report.feedback_for_fixer}"
        else:
            reason = f"verification was inconclusive, so the fix cannot be trusted yet:\n{report.feedback_for_fixer}"
        return {"verification": dump, "attempts": attempts, **escalate(state, "agent:verifier", reason, patch)}

    def review(state: RunState) -> dict:
        change_id, attempt = state["change_id"], state["attempt"]
        ctx = IncidentContext(**state["ctx"])
        investigation = InvestigationReport(**state["investigation"])
        verification = VerificationReport(**state["verification"])
        sandbox = runtime.sandbox_for(change_id, state["branch"])

        store.record_agent_step(change_id, ORCHESTRATOR, "reviewing", "agent.reviewing", {"attempts": state["attempts"]})
        tools = tools_for("critic", change_id, sandbox)
        review_ = runtime.critic.review(ctx, investigation, FixProposal(**state["proposal"]), verification, tools)
        dump = review_.model_dump(mode="json")
        attempts = [dict(item) for item in state["attempts"]]
        attempts[-1]["review"] = {
            "decision": review_.decision,
            "risk": review_.risk,
            "concerns": [{"severity": c.severity, "text": c.text} for c in review_.concerns],
        }
        patch = {"review": dump, "attempts": attempts}
        store.record_agent_step(
            change_id, "agent:critic", "reviewing", "agent.reviewed", patch,
            note=f"attempt {attempt}: {review_.decision}, risk {review_.risk}",
        )

        if review_.decision == "approve_for_human":
            return {"review": dump, "attempts": attempts, "next_step": "package"}
        if review_.decision == "send_back" and attempt < runtime.max_attempts:
            return {"review": dump, "attempts": attempts, "feedback": review_.feedback_for_fixer, "next_step": "retry"}
        reason = (
            f"the Critic sent the fix back on all {attempt} attempt(s):\n{review_.feedback_for_fixer}"
            if review_.decision == "send_back"
            else f"the Critic blocked the fix:\n{review_.feedback_for_fixer}"
        )
        return {"review": dump, "attempts": attempts, **escalate(state, "agent:critic", reason, patch)}

    def retry(state: RunState) -> dict:
        return {"attempt": state["attempt"] + 1, "next_step": "fix"}

    def package(state: RunState) -> dict:
        verification = VerificationReport(**state["verification"])
        review_ = state["review"]
        improvement = float(verification.alert_metric.get("improvement", 0.0))
        notes = (
            f"agent run: verified on attempt {state['attempt']} "
            f"(alert metric {verification.alert_metric.get('before')} -> {verification.alert_metric.get('after')}, "
            f"threshold {verification.alert_metric.get('threshold')}); critic: {review_['decision']}, risk {review_['risk']}"
        )
        # The approver must be able to see exactly what was verified; if the diff cannot be read the run
        # fails (and escalates) rather than offering a change nobody can inspect.
        diff = runtime.sandbox_for(state["change_id"], state["branch"]).diff()
        patch = {
            "outcome": "ready_for_approval",
            "branch": state["branch"],
            "diff": stored_diff(diff),
            "changed_files": review_.get("changed_files", []),
            "risk": review_["risk"],
            "summary": notes,
            "budget": runtime.budget_for(state["change_id"]).usage(),
        }
        store.finish_agent_run(
            state["change_id"], VERIFIER_ACTOR, verification.deploy_url, round(improvement * 100, 1), notes, patch
        )
        return {"outcome": "ready_for_approval", "next_step": "end"}

    def crash(state: RunState) -> dict:
        try:
            store.record_agent_step(
                state["change_id"], ORCHESTRATOR, "escalated", "agent.failed", {},
                escalation_reason=f"the agent run failed unexpectedly ({state['crashed']})",
            )
        except ValueError:
            logger.warning("could-not-record-agent-failure", extra={"change_id": state["change_id"]})
        return {"outcome": "escalated", "next_step": "end"}

    # ------------------------------------------------------------------- graph

    def route(state: RunState) -> str:
        return "crash" if state.get("crashed") else state.get("next_step", "end")

    graph = StateGraph(RunState)
    for name, node in {
        "investigate": investigate, "fix": fix, "verify": verify, "review": review, "retry": retry, "package": package,
    }.items():
        graph.add_node(name, guarded(name, node))
    graph.add_node("crash", crash)

    graph.set_entry_point("investigate")
    graph.add_conditional_edges("investigate", route, {"fix": "fix", "end": END, "crash": "crash"})
    graph.add_conditional_edges("fix", route, {"verify": "verify", "end": END, "crash": "crash"})
    graph.add_conditional_edges("verify", route, {"review": "review", "retry": "retry", "end": END, "crash": "crash"})
    graph.add_conditional_edges("review", route, {"package": "package", "retry": "retry", "end": END, "crash": "crash"})
    graph.add_edge("retry", "fix")
    graph.add_conditional_edges("package", route, {"end": END, "crash": "crash"})
    graph.add_edge("crash", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())
