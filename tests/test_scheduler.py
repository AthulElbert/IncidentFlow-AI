import json
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.apm_alert_source import MockAPMAlertSource
from app.adapters.apm_client import MockAPMClient
from app.adapters.jenkins_client import MockJenkinsClient
from app.adapters.jira_client import MockJiraClient
from app.adapters.pr_client import MockPRClient
from app.services.change_control import ChangeControlStore
from app.services.dev_fix_executor import DevFixExecutor
from app.services.pipeline import SupportAgentPipeline
from app.services.pr_preparer import PRPreparationService
from app.services.scheduler import AlertDedupStore, IncidentScheduler
from app.services.triage_agent import TriageAgent


def test_scheduler_run_once_processes_and_dedups_alerts(tmp_path: Path):
    queue_file = tmp_path / "apm_alert_queue.json"
    dedup_file = tmp_path / "processed_alerts.json"
    kb_file = tmp_path / "incident_history.json"
    change_file = tmp_path / "change_records.json"

    kb_file.write_text("[]", encoding="utf-8")
    change_file.write_text("[]", encoding="utf-8")
    queue_payload = [
        {
            "source_alert_id": "a-1",
            "service": "payments-service",
            "metric": "latency_p99_ms",
            "value": 1200,
            "threshold": 800,
            "environment": "prod",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": "Latency high in checkout",
        }
    ]
    queue_file.write_text(json.dumps(queue_payload), encoding="utf-8")

    change_store = ChangeControlStore(str(change_file))
    pipeline = SupportAgentPipeline(
        kb_file=str(kb_file),
        jira_client=MockJiraClient(project_key="SUP"),
        jenkins_client=MockJenkinsClient(),
        jira_mode="mock",
        jenkins_mode="mock",
        change_store=change_store,
        triage_agent=TriageAgent(mode="heuristic"),
    )
    scheduler = IncidentScheduler(
        alert_source=MockAPMAlertSource(str(queue_file)),
        pipeline=pipeline,
        change_store=change_store,
        pr_preparer=PRPreparationService(
            pr_client=MockPRClient(),
            test_mode="mock",
            repo_root=str(tmp_path),
            code_change_mode="spec",
        ),
        dev_executor=DevFixExecutor(
            jenkins_client=MockJenkinsClient(),
            apm_client=MockAPMClient(),
            min_apm_improvement_pct=5.0,
            require_smoke_tests=True,
        ),
        jenkins_client=MockJenkinsClient(),
        dedup_store=AlertDedupStore(str(dedup_file)),
        poll_interval_seconds=30,
        auto_remediation_mode="assistive",
    )

    scheduler.run_once()
    assert len(change_store.list_changes()) == 1

    # Same queue item should be deduped on second cycle.
    scheduler.run_once()
    assert len(change_store.list_changes()) == 1
