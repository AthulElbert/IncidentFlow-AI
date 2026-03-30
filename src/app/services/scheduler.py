import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.apm_alert_source import APMAlertSource
from app.adapters.jenkins_client import JenkinsClient
from app.models.schemas import ApprovalDecisionRequest, PromoteRequest
from app.services.change_control import ChangeControlStore
from app.services.dev_fix_executor import DevFixExecutor
from app.services.pipeline import SupportAgentPipeline
from app.services.pr_preparer import PRPreparationService

logger = logging.getLogger("app.scheduler")


class AlertDedupStore:
    def __init__(self, file_path: str) -> None:
        self.path = Path(file_path)
        self._lock = threading.Lock()
        self._seen = self._load()

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, list):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self._seen), indent=2), encoding="utf-8")

    def contains(self, alert_id: str) -> bool:
        with self._lock:
            return alert_id in self._seen

    def add(self, alert_id: str) -> None:
        with self._lock:
            self._seen.add(alert_id)
            self._persist()


@dataclass(frozen=True)
class SchedulerStats:
    runs: int = 0
    alerts_seen: int = 0
    alerts_processed: int = 0
    alerts_skipped_dedup: int = 0
    alerts_failed: int = 0
    last_run_at: str = ""
    last_error: str = ""


class IncidentScheduler:
    def __init__(
        self,
        alert_source: APMAlertSource,
        pipeline: SupportAgentPipeline,
        change_store: ChangeControlStore,
        pr_preparer: PRPreparationService,
        dev_executor: DevFixExecutor,
        jenkins_client: JenkinsClient,
        dedup_store: AlertDedupStore,
        poll_interval_seconds: int = 30,
        auto_remediation_mode: str = "assistive",
        safe_auto_issue_types: list[str] | None = None,
        auto_promote_on_policy_pass: bool = False,
        scheduler_actor: str = "scheduler-agent",
        scheduler_approver_actor: str = "scheduler-approver",
        scheduler_release_actor: str = "scheduler-release",
    ) -> None:
        self.alert_source = alert_source
        self.pipeline = pipeline
        self.change_store = change_store
        self.pr_preparer = pr_preparer
        self.dev_executor = dev_executor
        self.jenkins_client = jenkins_client
        self.dedup_store = dedup_store
        self.poll_interval_seconds = max(5, poll_interval_seconds)
        self.auto_remediation_mode = auto_remediation_mode
        self.safe_auto_issue_types = {item.upper() for item in (safe_auto_issue_types or [])}
        self.auto_promote_on_policy_pass = auto_promote_on_policy_pass
        self.scheduler_actor = scheduler_actor
        self.scheduler_approver_actor = scheduler_approver_actor
        self.scheduler_release_actor = scheduler_release_actor

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = SchedulerStats()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="incident-scheduler", daemon=True)
        self._thread.start()
        logger.info("scheduler-started", extra={"poll_interval_seconds": self.poll_interval_seconds})

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("scheduler-stopped")

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "poll_interval_seconds": self.poll_interval_seconds,
            "auto_remediation_mode": self.auto_remediation_mode,
            "stats": self._stats.__dict__,
        }

    def run_once(self) -> dict:
        self._process_cycle()
        return self.status()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._process_cycle()
            self._stop_event.wait(self.poll_interval_seconds)

    def _process_cycle(self) -> None:
        stats = self._stats.__dict__.copy()
        stats["runs"] = int(stats.get("runs", 0)) + 1
        stats["last_run_at"] = datetime.now(timezone.utc).isoformat()
        try:
            alerts = self.alert_source.fetch_open_alerts(limit=20)
            stats["alerts_seen"] = int(stats.get("alerts_seen", 0)) + len(alerts)
            for alert in alerts:
                if self.dedup_store.contains(alert.source_alert_id):
                    stats["alerts_skipped_dedup"] = int(stats.get("alerts_skipped_dedup", 0)) + 1
                    continue
                try:
                    response = self.pipeline.process_event(alert.event)
                    change_id = str(response.metadata.get("change_id", ""))
                    if not change_id:
                        raise ValueError("pipeline response missing change_id")
                    self.dedup_store.add(alert.source_alert_id)
                    stats["alerts_processed"] = int(stats.get("alerts_processed", 0)) + 1
                    self._auto_remediate(change_id)
                except Exception as exc:
                    stats["alerts_failed"] = int(stats.get("alerts_failed", 0)) + 1
                    stats["last_error"] = str(exc)
                    logger.exception("scheduler-alert-processing-failed", extra={"alert_id": alert.source_alert_id})
        except Exception as exc:
            stats["last_error"] = str(exc)
            logger.exception("scheduler-cycle-failed")
        self._stats = SchedulerStats(**stats)

    def _auto_remediate(self, change_id: str) -> None:
        mode = self.auto_remediation_mode
        if mode == "assistive":
            return

        record = self.change_store.get_change(change_id)
        if not record:
            return

        prepared = self.pr_preparer.prepare(record, requested_by=self.scheduler_actor, comment="scheduler auto flow")
        self.change_store.record_pr_preparation(
            change_id=change_id,
            generated_by=self.scheduler_actor,
            pr_status=prepared.pr.status,
            pr_url=prepared.pr.url,
            pr_branch=prepared.pr.branch,
            pr_title=prepared.pr.title,
            pr_summary=prepared.pr_summary,
            patch_artifact_path=prepared.patch_artifact_path,
            patch_preview=prepared.patch_preview,
            local_branch_created=prepared.local_branch_created,
            local_branch_message=prepared.local_branch_message,
            code_change_status=prepared.code_change_status,
            code_change_message=prepared.code_change_message,
            sandbox_worktree_path=prepared.sandbox_worktree_path,
            changed_files=prepared.changed_files,
            commit_sha=prepared.commit_sha,
            push_status=prepared.push_status,
            test_evidence_status=prepared.test_evidence_status,
            test_command=prepared.test_command,
            test_output=prepared.test_output,
            test_pass_rate=prepared.test_pass_rate,
        )

        record = self.change_store.get_change(change_id)
        if not record:
            return

        issue_type_ok = record.issue_type.upper() in self.safe_auto_issue_types
        should_auto_execute = mode == "full_auto" or (mode == "safe_auto" and issue_type_ok)
        if not should_auto_execute:
            return

        exec_url, jenkins_status, apm_improvement, smoke_ok, validation_passed, notes = self.dev_executor.execute(record)
        updated = self.change_store.record_dev_execution(
            change_id=change_id,
            executed_by=self.scheduler_approver_actor,
            execution_url=exec_url,
            jenkins_status=jenkins_status,
            apm_improvement_pct=apm_improvement,
            smoke_tests_passed=smoke_ok,
            validation_passed=validation_passed,
            notes=notes,
        )
        if updated.deployment_state != "ready_for_prod":
            return

        decided = self.change_store.apply_decision(
            change_id=change_id,
            payload=ApprovalDecisionRequest(decision="approve", comment="auto-approved by scheduler"),
            decided_by=self.scheduler_approver_actor,
        )
        if not self.auto_promote_on_policy_pass or decided.deployment_state != "ready_for_prod":
            return

        prod = self.jenkins_client.trigger_prod_deploy(service=decided.service, change_id=decided.change_id)
        status = (prod.status or "UNKNOWN").upper()
        if status == "SUCCESS":
            promotion_result = "success"
        elif status in {"QUEUED", "RUNNING", "IN_PROGRESS"}:
            promotion_result = "queued"
        else:
            promotion_result = "failed"
        self.change_store.promote_change(
            change_id=change_id,
            payload=PromoteRequest(comment="auto-promoted by scheduler"),
            promoted_by=self.scheduler_release_actor,
            promotion_result=promotion_result,
            prod_deploy_url=prod.url,
        )
