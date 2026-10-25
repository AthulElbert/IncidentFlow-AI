import json
from datetime import datetime, timezone

from app.models.schemas import APMEvent
from app.services.change_control import ChangeControlStore
from app.services.pipeline import SupportAgentPipeline
from app.services.triage_agent import TriageAgent


class BrokenJiraClient:
    def create_ticket(self, summary: str, description: str, labels: list[str]):
        raise RuntimeError("jira down")


class BrokenJenkinsClient:
    def trigger_dev_validation(self, service: str, issue_type: str):
        raise RuntimeError("jenkins down")


def test_failed_integrations_are_recorded_as_failed_not_simulated(tmp_path):
    kb_file = tmp_path / "incident_history.json"
    kb_file.write_text(json.dumps([]), encoding="utf-8")

    change_file = tmp_path / "change_records.json"
    change_file.write_text(json.dumps([]), encoding="utf-8")

    store = ChangeControlStore(str(change_file))
    pipeline = SupportAgentPipeline(
        kb_file=str(kb_file),
        jira_client=BrokenJiraClient(),
        jenkins_client=BrokenJenkinsClient(),
        jira_mode="real",
        jenkins_mode="real",
        change_store=store,
        triage_agent=TriageAgent(mode="heuristic"),
    )

    event = APMEvent(
        service="payments-service",
        metric="latency_p99_ms",
        value=1400,
        threshold=800,
        environment="prod",
        timestamp=datetime.now(timezone.utc),
        message="High latency observed",
    )

    resp = pipeline.process_event(event)

    # No fabricated ticket or build: the response says plainly that both failed.
    assert resp.jira_ticket is None
    assert resp.jenkins_validation.status == "UNKNOWN"
    assert resp.jenkins_validation.url == ""
    assert resp.metadata["jira_mode"] == "failed"
    assert resp.metadata["jenkins_mode"] == "failed"
    assert len(resp.metadata["warnings"]) == 2
    assert resp.metadata["triage_mode"] == "heuristic"
    assert resp.metadata["approval_required"] is True

    change = store.get_change(resp.metadata["change_id"])
    assert change.jira_key is None
    assert change.jira_sync_status == "pending_retry"
    assert change.jenkins_status == "UNKNOWN"
    assert change.warning_count == 2
    # The failures keep the change away from production.
    assert any("warning_count" in reason for reason in change.policy_reasons)
    assert any("jenkins_status UNKNOWN" in reason for reason in change.policy_reasons)
