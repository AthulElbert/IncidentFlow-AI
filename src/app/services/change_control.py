import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Callable
from uuid import uuid4

from app.audit import PG_AUDIT_SCHEMA, AuditEntry, AuditLog, new_audit_entry, pg_insert_audit
from app.db import pg_connect
from app.events.bus import INCIDENT_EVENTS_TOPIC, Event, EventBus, make_event
from app.events.postgres import PG_EVENT_SCHEMA, pg_insert_event
from app.models.schemas import (
    UNSAFE_BUILD_STATES_FOR_PROD,
    ApprovalDecisionRequest,
    ChangeRecord,
    PromoteRequest,
)
from app.services.lifecycle import TERMINAL_STATES, assert_transition
from app.storage import atomic_write_text

logger = logging.getLogger("app.change_control")

_PROMOTION_TERMINAL_STATES = {"promoted_to_prod", "prod_promotion_failed"}
_AGENT_START_STATES = {"awaiting_dev_execution", "escalated"}


def agent_start_blocker(record: ChangeRecord) -> str | None:
    """Why an agent run cannot start on this change right now, or None. `begin_agent_run` and the dashboard share it."""
    if record.agent_run_status == "running":
        return "An agent run is already in progress for this change"
    if record.status != "pending_approval":
        return "Change decision already finalized"
    if not record.alert:
        return "No alert was recorded for this change, so there is nothing to investigate"
    if record.deployment_state not in _AGENT_START_STATES:
        return f"Agents cannot start from state {record.deployment_state}"
    return None


def decision_blocker(record: ChangeRecord, decision: str) -> str | None:
    """Why this decision cannot be recorded right now, or None. A policy failure is not a blocker: it is recorded."""
    if record.status != "pending_approval":
        return "Change is already decided"
    if decision == "approve" and record.deployment_state != "ready_for_prod":
        return "Change is not ready for approval; execute dev fix first"
    return None
# A change in one of these states is finished (or being deployed) and must not absorb new alerts.
_CLOSED_FOR_CORRELATION = TERMINAL_STATES | _PROMOTION_TERMINAL_STATES | {"promoting", "monitoring"}

_PG_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS change_records (
        change_id TEXT PRIMARY KEY,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        version INT NOT NULL DEFAULT 1
    )
    """,
    "ALTER TABLE change_records ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1",
    *PG_AUDIT_SCHEMA,
    *PG_EVENT_SCHEMA,
)


class ConcurrentModificationError(RuntimeError):
    """A change record was modified by someone else between read and write."""


@dataclass(frozen=True)
class PolicyConfig:
    min_confidence_for_prod: float = 0.80
    require_zero_warnings_for_prod: bool = True
    allowed_jenkins_states_for_prod: list[str] | None = None
    enforce_separation_of_duties: bool = True

    def __post_init__(self) -> None:
        if self.allowed_jenkins_states_for_prod is None:
            object.__setattr__(self, "allowed_jenkins_states_for_prod", ["SUCCESS"])
        unsafe = {item.upper() for item in self.allowed_jenkins_states_for_prod} & UNSAFE_BUILD_STATES_FOR_PROD
        if unsafe:
            raise ValueError(
                f"allowed_jenkins_states_for_prod cannot include non-terminal states: {sorted(unsafe)}"
            )


class ChangeControlStore:
    """Owns change records, their lifecycle, and the audit trail of every change to them.

    Each successful write also produces one audit entry and one `incident.events`
    event. With Postgres they are inserted in the same transaction as the state
    change, so a committed change always has its audit entry and event. With the
    JSON backend they are appended right after the state file is written and a
    failure there is logged rather than raised (acceptable for local use).
    """

    _MAX_WRITE_ATTEMPTS = 3

    def __init__(
        self,
        data_file: str,
        policy: PolicyConfig | None = None,
        storage_backend: str = "json",
        database_url: str = "",
        audit: AuditLog | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.data_path = Path(data_file)
        self._lock = Lock()
        self.policy = policy or PolicyConfig()
        self.storage_backend = storage_backend.strip().lower()
        self.database_url = database_url.strip()
        self.audit = audit
        self.bus = bus
        self._use_postgres = self.storage_backend == "postgres"
        self._schema_ready = False
        self._records: list[ChangeRecord] = self._load()

    # ------------------------------------------------------------------ policy

    def _evaluate_policy(self, record: ChangeRecord) -> list[str]:
        reasons: list[str] = []

        if record.confidence < self.policy.min_confidence_for_prod:
            reasons.append(
                f"confidence {record.confidence:.2f} below minimum {self.policy.min_confidence_for_prod:.2f}"
            )

        if self.policy.require_zero_warnings_for_prod and record.warning_count > 0:
            reasons.append(f"warning_count {record.warning_count} must be 0")

        allowed = {item.upper() for item in self.policy.allowed_jenkins_states_for_prod}
        if record.jenkins_status.upper() not in allowed:
            reasons.append(
                f"jenkins_status {record.jenkins_status} not in allowed set {sorted(list(allowed))}"
            )

        if record.issue_type == "unknown":
            reasons.append("issue_type unknown requires manual deep-dive")

        if record.dev_execution_status != "passed":
            reasons.append("dev execution evidence is not in passed state")

        return reasons

    # ----------------------------------------------------------------- queries

    def get_change(self, change_id: str) -> ChangeRecord | None:
        with self._lock:
            _, record = self._find(change_id)
            return record

    def list_changes(self, status: str | None = None) -> list[ChangeRecord]:
        with self._lock:
            if self._use_postgres:
                self._records = self._load_from_postgres()
            if not status:
                return list(self._records)
            return [item for item in self._records if item.status == status]

    def find_open_change(
        self,
        service: str,
        environment: str,
        within_minutes: float,
        now: datetime | None = None,
    ) -> ChangeRecord | None:
        """Most recently touched unapproved change for this service/environment inside the window."""
        if within_minutes <= 0:
            return None
        reference = now or datetime.now(timezone.utc)
        cutoff = reference - timedelta(minutes=within_minutes)
        candidates = [
            item
            for item in self.list_changes(status="pending_approval")
            if item.service == service
            and item.environment == environment
            and item.deployment_state not in _CLOSED_FOR_CORRELATION
            and (item.last_alert_at or item.created_at) >= cutoff
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.last_alert_at or item.created_at)

    # --------------------------------------------------------------- mutations

    def create_change(
        self,
        incident_id: str,
        service: str,
        environment: str,
        summary: str,
        jira_key: str | None,
        jenkins_job_url: str,
        proposed_actions: list[str],
        triage_mode_used: str,
        triage_hypothesis_steps: list[str],
        issue_type: str,
        confidence: float,
        warning_count: int,
        jenkins_status: str,
        jira_sync_status: str = "synced",
        actor: str = "pipeline",
        alert: dict | None = None,
    ) -> ChangeRecord:
        with self._lock:
            seed_record = ChangeRecord(
                change_id=f"CHG-{str(uuid4()).split('-')[0].upper()}",
                incident_id=incident_id,
                service=service,
                environment=environment,
                summary=summary,
                jira_key=jira_key,
                jira_sync_status=jira_sync_status,
                jenkins_job_url=jenkins_job_url,
                proposed_actions=proposed_actions,
                triage_mode_used=triage_mode_used,
                triage_hypothesis_steps=triage_hypothesis_steps,
                issue_type=issue_type,
                confidence=confidence,
                warning_count=warning_count,
                jenkins_status=jenkins_status,
                alert=alert,
                created_at=datetime.now(timezone.utc),
            )
            reasons = self._evaluate_policy(seed_record)
            record = seed_record.model_copy(update={"policy_reasons": reasons})
            entry, event = self._emission(
                "change.created",
                actor,
                None,
                record,
                {"incident_id": incident_id, "issue_type": issue_type, "confidence": confidence},
            )

            if self._use_postgres:
                self._insert_postgres(record, entry, event)
            self._records.append(record)
            if not self._use_postgres:
                self._write_json()
                self._emit_after_commit(entry, event)
            return record

    def attach_alert(self, change_id: str, source_alert_id: str, metric: str, actor: str) -> ChangeRecord:
        """Fold another alert into an existing change instead of opening a new one."""

        def apply(record: ChangeRecord) -> ChangeRecord:
            if source_alert_id in record.related_alerts:
                return record
            return record.model_copy(
                update={
                    "related_alerts": [*record.related_alerts, source_alert_id],
                    "alert_count": record.alert_count + 1,
                    "last_alert_at": datetime.now(timezone.utc),
                }
            )

        return self._mutate(
            change_id,
            apply,
            action="alert.correlated",
            actor=actor,
            details={"source_alert_id": source_alert_id, "metric": metric},
        )

    def ensure_dev_executable(self, change_id: str) -> ChangeRecord:
        """Check a dev run is allowed BEFORE triggering a Jenkins build for it."""
        record = self.get_change(change_id)
        if record is None:
            raise ValueError("Change not found")
        if record.status != "pending_approval":
            raise ValueError("Change decision already finalized")
        if record.deployment_state in _PROMOTION_TERMINAL_STATES | {"promoting"}:
            raise ValueError("Change already promoted; dev execution not allowed")
        if record.deployment_state == "dev_fix_in_progress":
            raise ValueError("A dev build is already in progress; use refresh-status")
        if record.agent_run_status == "running":
            raise ValueError("An agent run is in progress for this change")
        return record

    def record_dev_execution(
        self,
        change_id: str,
        executed_by: str,
        execution_url: str,
        jenkins_status: str,
        apm_improvement_pct: float,
        smoke_tests_passed: bool,
        validation_passed: bool | None,
        notes: str,
    ) -> ChangeRecord:
        """Record a dev run. validation_passed=None means the Jenkins build is still running."""

        def apply(record: ChangeRecord) -> ChangeRecord:
            return self._with_dev_execution(
                record, executed_by, execution_url, jenkins_status, apm_improvement_pct,
                smoke_tests_passed, validation_passed, notes,
            )

        return self._mutate(
            change_id,
            apply,
            action="dev.recorded",
            actor=executed_by,
            details={
                "jenkins_status": jenkins_status,
                "validation_passed": validation_passed,
                "execution_url": execution_url,
            },
        )

    def _with_dev_execution(
        self,
        record: ChangeRecord,
        executed_by: str,
        execution_url: str,
        jenkins_status: str,
        apm_improvement_pct: float,
        smoke_tests_passed: bool,
        validation_passed: bool | None,
        notes: str,
    ) -> ChangeRecord:
        if record.status != "pending_approval":
            raise ValueError("Change decision already finalized")

        if record.deployment_state in _PROMOTION_TERMINAL_STATES | {"promoting"}:
            raise ValueError("Change already promoted; dev execution not allowed")

        if validation_passed is None:
            dev_execution_status = "in_progress"
            deployment_state = "dev_fix_in_progress"
        elif validation_passed:
            dev_execution_status = "passed"
            deployment_state = "ready_for_prod"
        else:
            dev_execution_status = "failed"
            deployment_state = "dev_fix_failed"

        updated = record.model_copy(
            update={
                "jenkins_status": jenkins_status,
                "dev_execution_status": dev_execution_status,
                "dev_execution_url": execution_url,
                "dev_apm_improvement_pct": apm_improvement_pct,
                "dev_smoke_tests_passed": smoke_tests_passed,
                "dev_notes": notes,
                "dev_executed_at": datetime.now(timezone.utc),
                "dev_executed_by": executed_by,
                "deployment_state": deployment_state,
            }
        )
        return updated.model_copy(update={"policy_reasons": self._evaluate_policy(updated)})

    # ---------------------------------------------------------- agent workflow

    # States the orchestrator may move a change into while an agent run is in progress.
    _AGENT_STATES = {"hypothesis_ready", "fixing", "verifying", "reviewing", "escalated"}

    def begin_agent_run(self, change_id: str, actor: str) -> ChangeRecord:
        """Reserve the change for one agent run. Refuses a second concurrent or finished run."""

        def apply(record: ChangeRecord) -> ChangeRecord:
            blocker = agent_start_blocker(record)
            if blocker:
                raise ValueError(blocker)
            runs = int(record.agent_run.get("run", 0)) + 1
            return record.model_copy(
                update={
                    "deployment_state": "investigating",
                    "agent_run_status": "running",
                    "agent_run": {"run": runs, "started_at": datetime.now(timezone.utc).isoformat(), "attempts": []},
                }
            )

        return self._mutate(change_id, apply, action="agent.started", actor=actor)

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
        """Move a running agent workflow to its next state and record what the agent produced."""
        if to_state not in self._AGENT_STATES:
            raise ValueError(f"'{to_state}' is not a state an agent run may set")

        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.agent_run_status != "running":
                raise ValueError("No agent run is in progress for this change")
            update: dict = {
                "deployment_state": to_state,
                "agent_run": {**record.agent_run, **(run_patch or {})},
            }
            if issue_type is not None:
                update["issue_type"] = issue_type
            if confidence is not None:
                update["confidence"] = confidence
            if to_state == "escalated":
                update["agent_run_status"] = "escalated"
                update["agent_run"]["escalation_reason"] = escalation_reason
                update["agent_run"]["finished_at"] = datetime.now(timezone.utc).isoformat()
            updated = record.model_copy(update=update)
            return updated.model_copy(update={"policy_reasons": self._evaluate_policy(updated)})

        return self._mutate(
            change_id,
            apply,
            action=action,
            actor=actor,
            details={
                "to_state": to_state,
                **({"reason": escalation_reason} if escalation_reason else {}),
                **({"note": note} if note else {}),
            },
        )

    def finish_agent_run(
        self,
        change_id: str,
        actor: str,
        execution_url: str,
        apm_improvement_pct: float,
        notes: str,
        run_patch: dict | None = None,
    ) -> ChangeRecord:
        """A verified, reviewed fix is ready for a human: record the evidence and open the approval gate.

        This is the only place an agent run reaches `ready_for_prod`. It does not approve anything:
        the change stays `pending_approval` and the policy gate runs again when a person approves.
        """

        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.agent_run_status != "running":
                raise ValueError("No agent run is in progress for this change")
            updated = self._with_dev_execution(
                record, actor, execution_url, "SUCCESS", apm_improvement_pct, True, True, notes
            )
            return updated.model_copy(
                update={
                    "agent_run_status": "ready_for_approval",
                    "agent_run": {
                        **record.agent_run,
                        **(run_patch or {}),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )

        return self._mutate(change_id, apply, action="agent.ready_for_approval", actor=actor)

    # ---- shadow runs: the same agents, observing only. Nothing here moves the change's state. ----

    def begin_shadow_run(self, change_id: str, actor: str) -> ChangeRecord:
        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.shadow.get("status") == "running":
                raise ValueError("A shadow run is already in progress for this change")
            if record.agent_run_status == "running":
                raise ValueError("An agent run is in progress for this change")
            if record.status != "pending_approval":
                raise ValueError("Change decision already finalized")
            if not record.alert:
                raise ValueError("No alert was recorded for this change, so there is nothing to investigate")
            runs = int(record.shadow.get("run", 0)) + 1
            started = datetime.now(timezone.utc).isoformat()
            return record.model_copy(update={"shadow": {"run": runs, "status": "running", "started_at": started, "attempts": []}})

        return self._mutate(change_id, apply, action="shadow.started", actor=actor)

    def record_shadow_step(
        self,
        change_id: str,
        actor: str,
        action: str,
        patch: dict | None = None,
        escalation_reason: str = "",
        note: str = "",
        escalated: bool = False,
    ) -> ChangeRecord:
        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.shadow.get("status") != "running":
                raise ValueError("No shadow run is in progress for this change")
            shadow = {**record.shadow, **(patch or {})}
            if escalated:
                shadow.update(
                    status="escalated", escalation_reason=escalation_reason, finished_at=datetime.now(timezone.utc).isoformat()
                )
            return record.model_copy(update={"shadow": shadow})

        details = {**({"reason": escalation_reason} if escalation_reason else {}), **({"note": note} if note else {})}
        return self._mutate(change_id, apply, action=f"shadow.{action}", actor=actor, details=details)

    def finish_shadow_run(self, change_id: str, actor: str, patch: dict | None = None) -> ChangeRecord:
        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.shadow.get("status") != "running":
                raise ValueError("No shadow run is in progress for this change")
            shadow = {**record.shadow, **(patch or {}), "status": "ready", "finished_at": datetime.now(timezone.utc).isoformat()}
            return record.model_copy(update={"shadow": shadow})

        return self._mutate(change_id, apply, action="shadow.ready", actor=actor)

    def recover_interrupted_runs(self, actor: str = "system") -> list[str]:
        """After a restart no run is still going: escalate any that were left `running`."""
        recovered: list[str] = []
        for record in self.list_changes():
            if record.shadow.get("status") == "running":
                try:
                    self.record_shadow_step(
                        record.change_id, actor, "interrupted", escalation_reason="the shadow run was interrupted by a restart",
                        escalated=True,
                    )
                    recovered.append(record.change_id)
                except ValueError:
                    pass
            if record.agent_run_status != "running":
                continue
            try:
                self.record_agent_step(
                    record.change_id,
                    actor,
                    "escalated",
                    "agent.interrupted",
                    escalation_reason="the agent run was interrupted by a restart; run it again",
                )
                recovered.append(record.change_id)
            except ValueError:
                continue
        return recovered

    def apply_decision(
        self,
        change_id: str,
        payload: ApprovalDecisionRequest,
        decided_by: str,
    ) -> ChangeRecord:
        def apply(record: ChangeRecord) -> ChangeRecord:
            blocker = decision_blocker(record, payload.decision)
            if blocker:
                raise ValueError(blocker)

            decided_at = datetime.now(timezone.utc)

            if payload.decision != "approve":
                return record.model_copy(
                    update={
                        "status": "rejected",
                        "deployment_state": "closed_rejected",
                        "decided_at": decided_at,
                        "decided_by": decided_by,
                        "decision_comment": payload.comment,
                    }
                )

            reasons = self._evaluate_policy(record)
            return record.model_copy(
                update={
                    "status": "approved",
                    "deployment_state": "ready_for_prod" if not reasons else "blocked_by_policy",
                    "policy_reasons": reasons,
                    "decided_at": decided_at,
                    "decided_by": decided_by,
                    "decision_comment": payload.comment,
                }
            )

        verb = "approved" if payload.decision == "approve" else "rejected"
        return self._mutate(
            change_id,
            apply,
            action=f"decision.{verb}",
            actor=decided_by,
            details={"comment": payload.comment},
        )

    def promotion_blocker(self, record: ChangeRecord, promoted_by: str | None = None) -> str | None:
        """Why this change cannot be promoted now, or None. With `promoted_by` it also applies separation of duties."""
        if record.deployment_state == "promoting":
            return "Change is already being promoted"
        if record.deployment_state in _PROMOTION_TERMINAL_STATES:
            return "Change already promoted"
        if record.status != "approved":
            return "Change must be approved before promotion"
        if record.deployment_state != "ready_for_prod":
            return "Change is not ready_for_prod"
        if (
            promoted_by is not None
            and self.policy.enforce_separation_of_duties
            and record.decided_by
            and record.decided_by == promoted_by
        ):
            return "Promotion requires a different actor than the approver"
        reasons = self._evaluate_policy(record)
        if reasons:
            return "Promotion blocked by policy: " + "; ".join(reasons)
        return None

    def policy_reasons(self, record: ChangeRecord) -> list[str]:
        """What the policy gate would say about this change right now (empty means it passes)."""
        return self._evaluate_policy(record)

    def begin_promotion(self, change_id: str, payload: PromoteRequest, promoted_by: str) -> ChangeRecord:
        """Validate and reserve a promotion BEFORE any deploy is triggered.

        Moves the change to `promoting` atomically, so a second concurrent request
        or a retry is rejected instead of triggering another deploy.
        """

        def apply(record: ChangeRecord) -> ChangeRecord:
            blocker = self.promotion_blocker(record, promoted_by)
            if blocker:
                raise ValueError(blocker)

            return record.model_copy(
                update={
                    "deployment_state": "promoting",
                    "promoted_by": promoted_by,
                    "promotion_comment": payload.comment,
                    "promotion_key": uuid4().hex,
                    "promotion_result": None,
                }
            )

        return self._mutate(
            change_id,
            apply,
            action="promotion.reserved",
            actor=promoted_by,
            details={"comment": payload.comment},
        )

    def finalize_promotion(
        self,
        change_id: str,
        promotion_result: str,
        prod_deploy_url: str,
        note: str = "",
        actor: str = "system",
    ) -> ChangeRecord:
        """Apply the outcome of the prod deploy.

        Only a confirmed `success` makes the change `promoted_to_prod`. `queued`
        keeps it in `promoting` until the build is confirmed.
        """

        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.deployment_state != "promoting":
                raise ValueError("Change is not in promoting state")

            comment = record.promotion_comment or ""
            if note:
                comment = f"{comment} | {note}" if comment else note

            update: dict = {
                "promotion_result": promotion_result,
                "prod_deploy_url": prod_deploy_url or record.prod_deploy_url,
                "promotion_comment": comment or None,
            }
            if promotion_result == "success":
                update["deployment_state"] = "promoted_to_prod"
                update["promoted_at"] = datetime.now(timezone.utc)
            elif promotion_result == "queued":
                update["deployment_state"] = "promoting"
            else:
                update["deployment_state"] = "prod_promotion_failed"
            return record.model_copy(update=update)

        return self._mutate(
            change_id,
            apply,
            action=f"promotion.{promotion_result}",
            actor=actor,
            details={"prod_deploy_url": prod_deploy_url, "note": note},
        )

    def record_pr_preparation(
        self,
        change_id: str,
        generated_by: str,
        pr_status: str,
        pr_url: str,
        pr_branch: str,
        pr_title: str,
        pr_summary: str,
        patch_artifact_path: str,
        patch_preview: str,
        local_branch_created: bool,
        local_branch_message: str,
        code_change_status: str,
        code_change_message: str,
        sandbox_worktree_path: str,
        changed_files: list[str],
        commit_sha: str,
        push_status: str,
        test_evidence_status: str,
        test_command: str,
        test_output: str,
        test_pass_rate: float,
    ) -> ChangeRecord:
        def apply(record: ChangeRecord) -> ChangeRecord:
            return record.model_copy(
                update={
                    "pr_status": "draft_created" if pr_status.lower() == "draft" else "failed",
                    "pr_url": pr_url,
                    "pr_branch": pr_branch,
                    "pr_title": pr_title,
                    "pr_summary": pr_summary,
                    "pr_generated_at": datetime.now(timezone.utc),
                    "pr_generated_by": generated_by,
                    "patch_artifact_path": patch_artifact_path,
                    "patch_preview": patch_preview,
                    "local_branch_created": local_branch_created,
                    "local_branch_message": local_branch_message,
                    "code_change_status": (
                        code_change_status
                        if code_change_status in {"not_started", "applied", "failed"}
                        else "failed"
                    ),
                    "code_change_message": code_change_message,
                    "sandbox_worktree_path": sandbox_worktree_path,
                    "changed_files": changed_files,
                    "commit_sha": commit_sha,
                    "push_status": push_status,
                    "test_evidence_status": test_evidence_status,
                    "test_command": test_command,
                    "test_output": test_output,
                    "test_pass_rate": test_pass_rate,
                    "test_run_at": datetime.now(timezone.utc),
                }
            )

        return self._mutate(
            change_id,
            apply,
            action="pr.prepared",
            actor=generated_by,
            details={
                "pr_branch": pr_branch,
                "code_change_status": code_change_status,
                "test_evidence_status": test_evidence_status,
            },
        )

    # ------------------------------------------------------- mutation plumbing

    def _find(self, change_id: str) -> tuple[int, ChangeRecord | None]:
        """Locate a record, re-reading it from the database first when using Postgres."""
        if self._use_postgres:
            fresh = self._fetch_one_postgres(change_id)
            for idx, record in enumerate(self._records):
                if record.change_id == change_id:
                    if fresh is None:
                        return idx, None
                    self._records[idx] = fresh
                    return idx, fresh
            if fresh is None:
                return -1, None
            self._records.append(fresh)
            return len(self._records) - 1, fresh

        for idx, record in enumerate(self._records):
            if record.change_id == change_id:
                return idx, record
        return -1, None

    def _mutate(
        self,
        change_id: str,
        apply: Callable[[ChangeRecord], ChangeRecord],
        action: str,
        actor: str,
        details: dict | None = None,
    ) -> ChangeRecord:
        """Read-modify-write one record under the lock, bumping its version.

        The new state must be a legal lifecycle transition. With Postgres the
        write is conditional on the version we read, so a concurrent writer in
        another process causes a retry instead of a lost update.
        """
        with self._lock:
            for _ in range(self._MAX_WRITE_ATTEMPTS):
                idx, current = self._find(change_id)
                if current is None:
                    raise ValueError("Change not found")

                updated = apply(current).model_copy(update={"version": current.version + 1})
                assert_transition(current.deployment_state, updated.deployment_state)
                entry, event = self._emission(action, actor, current, updated, details)
                try:
                    self._save(idx, updated, current.version, entry, event)
                except ConcurrentModificationError:
                    continue
                return updated
            raise ConcurrentModificationError(
                f"Change {change_id} was modified concurrently; retry the request"
            )

    def _emission(
        self,
        action: str,
        actor: str,
        before: ChangeRecord | None,
        after: ChangeRecord,
        details: dict | None,
    ) -> tuple[AuditEntry, Event]:
        from_state = before.deployment_state if before else None
        entry = new_audit_entry(
            actor=actor,
            action=action,
            entity_id=after.change_id,
            from_state=from_state,
            to_state=after.deployment_state,
            details=details,
        )
        event = make_event(
            INCIDENT_EVENTS_TOPIC,
            after.service,
            {
                "change_id": after.change_id,
                "incident_id": after.incident_id,
                "action": action,
                "actor": actor,
                "from_state": from_state,
                "to_state": after.deployment_state,
                "status": after.status,
                "version": after.version,
            },
        )
        return entry, event

    def _save(
        self,
        idx: int,
        updated: ChangeRecord,
        expected_version: int,
        entry: AuditEntry,
        event: Event,
    ) -> None:
        if self._use_postgres:
            self._update_postgres(updated, expected_version, entry, event)
        self._records[idx] = updated
        if not self._use_postgres:
            self._write_json()
            self._emit_after_commit(entry, event)

    def _emit_after_commit(self, entry: AuditEntry, event: Event) -> None:
        if self.audit is not None:
            try:
                self.audit.append(entry)
            except Exception:
                logger.exception("audit-append-failed", extra={"entity_id": entry.entity_id, "action": entry.action})
        if self.bus is not None:
            try:
                self.bus.publish(event)
            except Exception:
                logger.exception("event-publish-failed", extra={"entity_id": entry.entity_id, "action": entry.action})

    # ------------------------------------------------------------------- JSON

    def _load(self) -> list[ChangeRecord]:
        if self._use_postgres:
            return self._load_from_postgres()
        if not self.data_path.exists():
            return []
        raw = json.loads(self.data_path.read_text(encoding="utf-8-sig"))
        return [ChangeRecord(**item) for item in raw]

    def _write_json(self) -> None:
        payload = [item.model_dump(mode="json") for item in self._records]
        atomic_write_text(self.data_path, json.dumps(payload, indent=2))

    # --------------------------------------------------------------- Postgres

    def _pg(self, work):
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                if not self._schema_ready:
                    for statement in _PG_SCHEMA:
                        cur.execute(statement)
                result = work(cur)
        self._schema_ready = True
        return result

    @staticmethod
    def _record_from_row(payload, version) -> ChangeRecord:
        data = payload if isinstance(payload, dict) else json.loads(payload)
        data["version"] = int(version)
        return ChangeRecord(**data)

    def _load_from_postgres(self) -> list[ChangeRecord]:
        def work(cur):
            cur.execute("SELECT payload, version FROM change_records ORDER BY created_at ASC")
            return cur.fetchall()

        return [self._record_from_row(row[0], row[1]) for row in self._pg(work)]

    def _fetch_one_postgres(self, change_id: str) -> ChangeRecord | None:
        def work(cur):
            cur.execute("SELECT payload, version FROM change_records WHERE change_id = %s", (change_id,))
            return cur.fetchone()

        row = self._pg(work)
        return self._record_from_row(row[0], row[1]) if row else None

    def _insert_postgres(self, record: ChangeRecord, entry: AuditEntry, event: Event) -> None:
        def work(cur):
            cur.execute(
                """
                INSERT INTO change_records (change_id, payload, created_at, version)
                VALUES (%s, %s::jsonb, %s, %s)
                """,
                (
                    record.change_id,
                    json.dumps(record.model_dump(mode="json")),
                    record.created_at.isoformat(),
                    record.version,
                ),
            )
            pg_insert_audit(cur, entry)
            pg_insert_event(cur, event)

        self._pg(work)

    def _update_postgres(
        self,
        record: ChangeRecord,
        expected_version: int,
        entry: AuditEntry,
        event: Event,
    ) -> None:
        def work(cur):
            cur.execute(
                """
                UPDATE change_records
                SET payload = %s::jsonb, version = %s
                WHERE change_id = %s AND version = %s
                """,
                (
                    json.dumps(record.model_dump(mode="json")),
                    record.version,
                    record.change_id,
                    expected_version,
                ),
            )
            if cur.rowcount != 1:
                # Raising here rolls the transaction back before any audit/event row is written.
                raise ConcurrentModificationError(record.change_id)
            pg_insert_audit(cur, entry)
            pg_insert_event(cur, event)

        self._pg(work)

    def _pg_connect(self):
        return pg_connect(self.database_url)
