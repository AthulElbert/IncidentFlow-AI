import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.models.schemas import ApprovalDecisionRequest, ChangeRecord, PromoteRequest


@dataclass(frozen=True)
class PolicyConfig:
    min_confidence_for_prod: float = 0.80
    require_zero_warnings_for_prod: bool = True
    allowed_jenkins_states_for_prod: list[str] | None = None

    def __post_init__(self) -> None:
        if self.allowed_jenkins_states_for_prod is None:
            object.__setattr__(self, "allowed_jenkins_states_for_prod", ["QUEUED", "SUCCESS"])


class ChangeControlStore:
    def __init__(self, data_file: str, policy: PolicyConfig | None = None) -> None:
        self.data_path = Path(data_file)
        self._lock = Lock()
        self.policy = policy or PolicyConfig()
        self._records: list[ChangeRecord] = self._load()

    def _load(self) -> list[ChangeRecord]:
        if not self.data_path.exists():
            return []
        raw = json.loads(self.data_path.read_text(encoding="utf-8-sig"))
        return [ChangeRecord(**item) for item in raw]

    def _persist(self) -> None:
        payload = [item.model_dump(mode="json") for item in self._records]
        self.data_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

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

    def create_change(
        self,
        incident_id: str,
        service: str,
        environment: str,
        summary: str,
        jira_key: str,
        jenkins_job_url: str,
        proposed_actions: list[str],
        triage_mode_used: str,
        triage_hypothesis_steps: list[str],
        issue_type: str,
        confidence: float,
        warning_count: int,
        jenkins_status: str,
    ) -> ChangeRecord:
        with self._lock:
            seed_record = ChangeRecord(
                change_id=f"CHG-{str(uuid4()).split('-')[0].upper()}",
                incident_id=incident_id,
                service=service,
                environment=environment,
                summary=summary,
                jira_key=jira_key,
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

            self._records.append(record)
            self._persist()
            return record

    def get_change(self, change_id: str) -> ChangeRecord | None:
        with self._lock:
            for record in self._records:
                if record.change_id == change_id:
                    return record
        return None

    def list_changes(self, status: str | None = None) -> list[ChangeRecord]:
        with self._lock:
            if not status:
                return list(self._records)
            return [item for item in self._records if item.status == status]

    def record_dev_execution(
        self,
        change_id: str,
        executed_by: str,
        execution_url: str,
        jenkins_status: str,
        apm_improvement_pct: float,
        smoke_tests_passed: bool,
        validation_passed: bool,
        notes: str,
    ) -> ChangeRecord:
        with self._lock:
            for idx, record in enumerate(self._records):
                if record.change_id != change_id:
                    continue

                if record.status != "pending_approval":
                    raise ValueError("Change decision already finalized")

                if record.deployment_state in {"promoted_to_prod", "prod_promotion_failed"}:
                    raise ValueError("Change already promoted; dev execution not allowed")

                dev_execution_status = "passed" if validation_passed else "failed"
                deployment_state = "ready_for_prod" if validation_passed else "dev_fix_failed"

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
                updated = updated.model_copy(update={"policy_reasons": self._evaluate_policy(updated)})

                self._records[idx] = updated
                self._persist()
                return updated

        raise ValueError("Change not found")

    def apply_decision(
        self,
        change_id: str,
        payload: ApprovalDecisionRequest,
        decided_by: str,
    ) -> ChangeRecord:
        with self._lock:
            for idx, record in enumerate(self._records):
                if record.change_id != change_id:
                    continue

                if record.status != "pending_approval":
                    raise ValueError("Change is already decided")

                decided_at = datetime.now(timezone.utc)
                is_approved = payload.decision == "approve"

                if not is_approved:
                    updated = record.model_copy(
                        update={
                            "status": "rejected",
                            "deployment_state": "closed_rejected",
                            "decided_at": decided_at,
                            "decided_by": decided_by,
                            "decision_comment": payload.comment,
                        }
                    )
                    self._records[idx] = updated
                    self._persist()
                    return updated

                if record.deployment_state != "ready_for_prod":
                    raise ValueError("Change is not ready for approval; execute dev fix first")

                reasons = self._evaluate_policy(record)
                deployment_state = "ready_for_prod" if not reasons else "blocked_by_policy"
                updated = record.model_copy(
                    update={
                        "status": "approved",
                        "deployment_state": deployment_state,
                        "policy_reasons": reasons,
                        "decided_at": decided_at,
                        "decided_by": decided_by,
                        "decision_comment": payload.comment,
                    }
                )

                self._records[idx] = updated
                self._persist()
                return updated

        raise ValueError("Change not found")

    def promote_change(
        self,
        change_id: str,
        payload: PromoteRequest,
        promoted_by: str,
        promotion_result: str,
        prod_deploy_url: str,
    ) -> ChangeRecord:
        with self._lock:
            for idx, record in enumerate(self._records):
                if record.change_id != change_id:
                    continue

                if record.deployment_state in {"promoted_to_prod", "prod_promotion_failed"}:
                    raise ValueError("Change already promoted")

                if record.status != "approved":
                    raise ValueError("Change must be approved before promotion")

                if record.deployment_state != "ready_for_prod":
                    raise ValueError("Change is not ready_for_prod")

                deployment_state = (
                    "promoted_to_prod" if promotion_result in {"queued", "success"} else "prod_promotion_failed"
                )
                promoted_at = datetime.now(timezone.utc)
                updated = record.model_copy(
                    update={
                        "deployment_state": deployment_state,
                        "promoted_at": promoted_at,
                        "promoted_by": promoted_by,
                        "promotion_comment": payload.comment,
                        "promotion_result": promotion_result,
                        "prod_deploy_url": prod_deploy_url,
                    }
                )

                self._records[idx] = updated
                self._persist()
                return updated

        raise ValueError("Change not found")
