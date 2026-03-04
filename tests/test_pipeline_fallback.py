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


def test_pipeline_runtime_fallbacks(tmp_path):
    kb_file = tmp_path / "incident_history.json"
    kb_file.write_text(json.dumps([]), encoding="utf-8")

    change_file = tmp_path / "change_records.json"
    change_file.write_text(json.dumps([]), encoding="utf-8")

    pipeline = SupportAgentPipeline(
        kb_file=str(kb_file),
        jira_client=BrokenJiraClient(),
        jenkins_client=BrokenJenkinsClient(),
        jira_mode="real",
        jenkins_mode="real",
        change_store=ChangeControlStore(str(change_file)),
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

    assert resp.metadata["jira_mode"] == "mock-fallback"
    assert resp.metadata["jenkins_mode"] == "mock-fallback"
    assert resp.metadata["triage_mode"] == "heuristic"
    assert len(resp.metadata["warnings"]) == 2
    assert resp.jira_ticket.key.startswith("SUP-")
    assert resp.jenkins_validation.status == "QUEUED"
    assert resp.metadata["approval_required"] is True
    assert resp.metadata["change_id"].startswith("CHG-")
