import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable
from uuid import uuid4

from app.models.schemas import (
    UNSAFE_BUILD_STATES_FOR_PROD,
    ApprovalDecisionRequest,
    ChangeRecord,
    PromoteRequest,
)
from app.storage import atomic_write_text

_PROMOTION_TERMINAL_STATES = {"promoted_to_prod", "prod_promotion_failed"}

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
    _MAX_WRITE_ATTEMPTS = 3

    def __init__(
        self,
        data_file: str,
        policy: PolicyConfig | None = None,
        storage_backend: str = "json",
        database_url: str = "",
    ) -> None:
        self.data_path = Path(data_file)
        self._lock = Lock()
        self.policy = policy or PolicyConfig()
        self.storage_backend = storage_backend.strip().lower()
        self.database_url = database_url.strip()
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
                created_at=datetime.now(timezone.utc),
            )
            reasons = self._evaluate_policy(seed_record)
            record = seed_record.model_copy(update={"policy_reasons": reasons})

            if self._use_postgres:
                self._insert_postgres(record)
            self._records.append(record)
            if not self._use_postgres:
                self._write_json()
            return record

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

        return self._mutate(change_id, apply)

    def apply_decision(
        self,
        change_id: str,
        payload: ApprovalDecisionRequest,
        decided_by: str,
    ) -> ChangeRecord:
        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.status != "pending_approval":
                raise ValueError("Change is already decided")

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

            if record.deployment_state != "ready_for_prod":
                raise ValueError("Change is not ready for approval; execute dev fix first")

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

        return self._mutate(change_id, apply)

    def begin_promotion(self, change_id: str, payload: PromoteRequest, promoted_by: str) -> ChangeRecord:
        """Validate and reserve a promotion BEFORE any deploy is triggered.

        Moves the change to `promoting` atomically, so a second concurrent request
        or a retry is rejected instead of triggering another deploy.
        """

        def apply(record: ChangeRecord) -> ChangeRecord:
            if record.deployment_state == "promoting":
                raise ValueError("Change is already being promoted")
            if record.deployment_state in _PROMOTION_TERMINAL_STATES:
                raise ValueError("Change already promoted")
            if record.status != "approved":
                raise ValueError("Change must be approved before promotion")
            if record.deployment_state != "ready_for_prod":
                raise ValueError("Change is not ready_for_prod")

            if (
                self.policy.enforce_separation_of_duties
                and record.decided_by
                and record.decided_by == promoted_by
            ):
                raise ValueError("Promotion requires a different actor than the approver")

            reasons = self._evaluate_policy(record)
            if reasons:
                raise ValueError("Promotion blocked by policy: " + "; ".join(reasons))

            return record.model_copy(
                update={
                    "deployment_state": "promoting",
                    "promoted_by": promoted_by,
                    "promotion_comment": payload.comment,
                    "promotion_key": uuid4().hex,
                    "promotion_result": None,
                }
            )

        return self._mutate(change_id, apply)

    def finalize_promotion(
        self,
        change_id: str,
        promotion_result: str,
        prod_deploy_url: str,
        note: str = "",
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

        return self._mutate(change_id, apply)

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

        return self._mutate(change_id, apply)

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

    def _mutate(self, change_id: str, apply: Callable[[ChangeRecord], ChangeRecord]) -> ChangeRecord:
        """Read-modify-write one record under the lock, bumping its version.

        With Postgres the write is conditional on the version we read, so a
        concurrent writer in another process causes a retry instead of a lost update.
        """
        with self._lock:
            for _ in range(self._MAX_WRITE_ATTEMPTS):
                idx, current = self._find(change_id)
                if current is None:
                    raise ValueError("Change not found")

                updated = apply(current).model_copy(update={"version": current.version + 1})
                try:
                    self._save(idx, updated, expected_version=current.version)
                except ConcurrentModificationError:
                    continue
                return updated
            raise ConcurrentModificationError(
                f"Change {change_id} was modified concurrently; retry the request"
            )

    def _save(self, idx: int, updated: ChangeRecord, expected_version: int) -> None:
        if self._use_postgres:
            self._update_postgres(updated, expected_version)
        self._records[idx] = updated
        if not self._use_postgres:
            self._write_json()

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

    def _insert_postgres(self, record: ChangeRecord) -> None:
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

        self._pg(work)

    def _update_postgres(self, record: ChangeRecord, expected_version: int) -> None:
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
            return cur.rowcount

        if self._pg(work) != 1:
            raise ConcurrentModificationError(record.change_id)

    def _pg_connect(self):
        if not self.database_url:
            raise ValueError("STORAGE_BACKEND=postgres requires DATABASE_URL")
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover - import guard
            raise ValueError("Postgres backend requires 'psycopg' package") from exc
        return psycopg.connect(self.database_url)
