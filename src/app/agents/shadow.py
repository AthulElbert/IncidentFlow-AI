"""Shadow mode: run the same agents, record what they would have done, change nothing.

The graph talks to a store through `record_agent_step` and `finish_agent_run`. This adapter answers those calls by
writing to the change's `shadow` record, so a shadow run reuses the whole graph (and its tests) while it cannot move
the change's state, set its issue type or confidence, record dev-execution evidence, or open the approval gate.
"""
from app.models.schemas import ChangeRecord
from app.services.change_control import ChangeControlStore


class ShadowRecorder:
    def __init__(self, store: ChangeControlStore) -> None:
        self.store = store

    def record_agent_step(
        self,
        change_id: str,
        actor: str,
        to_state: str,
        action: str,
        run_patch: dict | None = None,
        issue_type: str | None = None,
        confidence: float | None = None,
        escalation_reason: str = "",
        note: str = "",
    ) -> ChangeRecord:
        # issue_type and confidence are deliberately dropped: they would overwrite what a person is looking at.
        return self.store.record_shadow_step(
            change_id, actor, action.removeprefix("agent."), run_patch, escalation_reason, note, escalated=to_state == "escalated"
        )

    def finish_agent_run(
        self, change_id: str, actor: str, execution_url: str, apm_improvement_pct: float, notes: str, run_patch: dict | None = None
    ) -> ChangeRecord:
        # No dev-execution evidence is recorded on the change; the run's own report holds the verification.
        return self.store.finish_shadow_run(change_id, actor, {**(run_patch or {}), "summary": notes})
