from datetime import datetime, timezone
from pathlib import Path

from app.adapters.pr_client import MockPRClient
from app.models.schemas import ChangeRecord
from app.services.pr_preparer import PRPreparationService


def test_pr_preparer_creates_draft_with_mock_test_evidence(tmp_path: Path):
    record = ChangeRecord(
        change_id="CHG-1234",
        incident_id="INC-1234",
        service="payments-service",
        environment="prod",
        summary="Latency fix proposal",
        jira_key="SUP-123",
        jenkins_job_url="https://jenkins/job/1",
        proposed_actions=["Tune connection pool", "Increase timeout by 20ms"],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=["step1"],
        issue_type="performance_degradation",
        confidence=0.91,
        warning_count=0,
        jenkins_status="QUEUED",
        created_at=datetime.now(timezone.utc),
    )

    service = PRPreparationService(
        pr_client=MockPRClient(repo_slug="demo/repo"),
        test_mode="mock",
        test_command="python -m pytest -q tests",
        repo_root=str(tmp_path),
        local_branch_mode="spec",
        patch_output_dir="generated_patches",
    )

    result = service.prepare(record, requested_by="approver-1", comment="please review")

    assert result.pr.status == "DRAFT"
    assert result.pr.branch.startswith("agent/chg-1234")
    assert "git.example.local" in result.pr.url
    assert result.local_branch_created is False
    assert "spec-only" in result.local_branch_message
    assert result.patch_artifact_path.endswith("CHG-1234.patch")
    assert (tmp_path / "generated_patches" / "CHG-1234.patch").exists()
    assert result.test_evidence_status == "passed"
    assert result.test_pass_rate == 1.0


def test_pr_preparer_generates_issue_specific_patch_templates(tmp_path: Path):
    service = PRPreparationService(
        pr_client=MockPRClient(repo_slug="demo/repo"),
        test_mode="mock",
        repo_root=str(tmp_path),
        local_branch_mode="spec",
        patch_output_dir="generated_patches",
    )

    issue_markers = {
        "performance_degradation": "perf_tuning:",
        "application_error": "error_guardrails:",
        "dependency_failure": "dependency_protection:",
        "resource_saturation": "resource_controls:",
        "unknown": "manual_triage:",
    }

    for issue_type, marker in issue_markers.items():
        record = ChangeRecord(
            change_id=f"CHG-{issue_type[:4].upper()}",
            incident_id=f"INC-{issue_type[:4].upper()}",
            service="payments-service",
            environment="prod",
            summary=f"{issue_type} proposal",
            jira_key="SUP-1",
            jenkins_job_url="https://jenkins/job/1",
            proposed_actions=["a1"],
            triage_mode_used="heuristic",
            triage_hypothesis_steps=["h1"],
            issue_type=issue_type,
            confidence=0.9,
            warning_count=0,
            jenkins_status="QUEUED",
            created_at=datetime.now(timezone.utc),
        )
        result = service.prepare(record, requested_by="approver-1")
        content = Path(result.patch_artifact_path).read_text(encoding="utf-8")
        assert marker in content
