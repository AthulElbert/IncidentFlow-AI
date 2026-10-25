from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

import pytest
from app.adapters.pr_client import MockPRClient
from app.models.schemas import ChangeRecord
from app.services.code_fix_agent import CodeFixAgent
from app.services.pr_preparer import PRPreparationService


class GoodFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return {
            "fixed_content": '{"latency_ms": 150, "error_probability": 0.01}\n',
            "summary": "Reduced injected latency and error probability to safe production values.",
        }


class StillUnsafeFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return {
            "fixed_content": '{"latency_ms": 900, "error_probability": 0.2}\n',
            "summary": "Slightly reduced values.",
        }


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


def test_pr_preparer_applies_sandbox_git_change(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git not available")

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Agent Bot"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    record = ChangeRecord(
        change_id="CHG-SBOX1",
        incident_id="INC-SBOX1",
        service="payments-service",
        environment="prod",
        summary="Sandbox branch change",
        jira_key="SUP-11",
        jenkins_job_url="https://jenkins/job/1",
        proposed_actions=["Tune timeout"],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=["h1"],
        issue_type="performance_degradation",
        confidence=0.91,
        warning_count=0,
        jenkins_status="QUEUED",
        created_at=datetime.now(timezone.utc),
    )
    service = PRPreparationService(
        pr_client=MockPRClient(repo_slug="demo/repo"),
        test_mode="mock",
        repo_root=str(tmp_path),
        code_change_mode="sandbox_git",
        code_change_allowed_paths="config/",
        code_change_auto_commit=False,
        code_change_auto_push=False,
    )
    result = service.prepare(record, requested_by="approver-1")

    assert result.code_change_status == "applied"
    assert result.sandbox_worktree_path
    assert "config/service-overrides.yaml" in result.changed_files
    applied_file = Path(result.sandbox_worktree_path) / "config" / "service-overrides.yaml"
    assert applied_file.exists()


def _init_repo_with_faulty_config(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Agent Bot"], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    faulty_dir = tmp_path / "faulty_project"
    faulty_dir.mkdir(parents=True, exist_ok=True)
    (faulty_dir / "app_config.json").write_text('{"latency_ms": 1800, "error_probability": 0.25}\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True, text=True)


def _fix_record() -> ChangeRecord:
    return ChangeRecord(
        change_id="CHG-FIX01",
        incident_id="INC-FIX01",
        service="faulty-checkout",
        environment="prod",
        summary="Checkout latency and errors above threshold",
        jira_key="SUP-99",
        jenkins_job_url="https://jenkins/job/1",
        proposed_actions=["Investigate injected fault config"],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=["h1"],
        issue_type="performance_degradation",
        confidence=0.9,
        warning_count=0,
        jenkins_status="QUEUED",
        created_at=datetime.now(timezone.utc),
    )


def test_pr_preparer_llm_code_fix_applies_verified_fix(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git not available")

    _init_repo_with_faulty_config(tmp_path)

    service = PRPreparationService(
        pr_client=MockPRClient(repo_slug="demo/repo"),
        test_mode="mock",
        repo_root=str(tmp_path),
        code_change_mode="sandbox_git",
        code_change_allowed_paths="faulty_project/",
        code_change_auto_commit=False,
        code_fix_mode="llm",
        code_fix_target_path="faulty_project/app_config.json",
        code_fix_agent=CodeFixAgent(llm_client=GoodFixLLM(), max_latency_ms=300.0, max_error_probability=0.02),
    )

    result = service.prepare(_fix_record(), requested_by="approver-1")

    assert result.code_change_status == "applied"
    assert "faulty_project/app_config.json" in result.changed_files
    applied_file = Path(result.sandbox_worktree_path) / "faulty_project" / "app_config.json"
    content = applied_file.read_text(encoding="utf-8")
    assert "150" in content
    assert "0.01" in content


def test_pr_preparer_llm_code_fix_falls_back_to_template_without_llm_key(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git not available")

    _init_repo_with_faulty_config(tmp_path)

    service = PRPreparationService(
        pr_client=MockPRClient(repo_slug="demo/repo"),
        test_mode="mock",
        repo_root=str(tmp_path),
        code_change_mode="sandbox_git",
        code_change_allowed_paths="config/,faulty_project/",
        code_change_auto_commit=False,
        code_fix_mode="llm",
        code_fix_target_path="faulty_project/app_config.json",
        code_fix_agent=CodeFixAgent(llm_client=None),
    )

    result = service.prepare(_fix_record(), requested_by="approver-1")

    assert result.code_change_status == "applied"
    assert "config/service-overrides.yaml" in result.changed_files
    faulty_file = Path(result.sandbox_worktree_path) / "faulty_project" / "app_config.json"
    assert faulty_file.read_text(encoding="utf-8") == '{"latency_ms": 1800, "error_probability": 0.25}\n'


def test_pr_preparer_llm_code_fix_rejects_unsafe_fix(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git not available")

    _init_repo_with_faulty_config(tmp_path)

    service = PRPreparationService(
        pr_client=MockPRClient(repo_slug="demo/repo"),
        test_mode="mock",
        repo_root=str(tmp_path),
        code_change_mode="sandbox_git",
        code_change_allowed_paths="faulty_project/",
        code_change_auto_commit=False,
        code_fix_mode="llm",
        code_fix_target_path="faulty_project/app_config.json",
        code_fix_agent=CodeFixAgent(llm_client=StillUnsafeFixLLM(), max_latency_ms=300.0, max_error_probability=0.02),
    )

    result = service.prepare(_fix_record(), requested_by="approver-1")

    assert result.code_change_status == "failed"
    assert result.changed_files == []
    untouched = Path(result.sandbox_worktree_path) / "faulty_project" / "app_config.json"
    assert untouched.read_text(encoding="utf-8") == '{"latency_ms": 1800, "error_probability": 0.25}\n'
