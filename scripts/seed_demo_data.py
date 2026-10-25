"""Reset data/*.json to a small curated demo dataset.

Run this before any live demo/rehearsal -- running pytest against this repo
also pollutes data/change_records.json and data/incident_history.json,
because several tests import the real app.main singleton instead of an
isolated tmp_path store. Re-run this script afterwards to get back to a
clean, known state.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app.models.schemas import ChangeRecord, IncidentRecord  # noqa: E402


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


incidents = [
    IncidentRecord(
        incident_id="INC-DEMO0001",
        service="payments-service",
        metric="latency_p99_ms",
        issue_type="performance_degradation",
        summary="[Recurring] payments-service - performance_degradation in prod",
        resolution="Increased connection pool size and enabled response caching; latency returned to baseline within 15 minutes.",
        runbook_actions=[
            "Check p95/p99 latency breakdown by endpoint in APM.",
            "Scale service replicas by +1 in dev and verify latency trend.",
            "Temporarily increase upstream timeout by 10-15% in dev config.",
        ],
        created_at=dt("2026-07-10T09:12:00"),
    ),
    IncidentRecord(
        incident_id="INC-DEMO0002",
        service="orders-service",
        metric="error_rate_pct",
        issue_type="application_error",
        summary="[New] orders-service - application_error in prod",
        resolution="Rolled back the faulty deployment and enabled the safe-mode feature flag for the checkout module.",
        runbook_actions=[
            "Inspect recent deployment diff and rollback candidate changes in dev.",
            "Enable safe-mode feature flags for failing module.",
            "Validate error-rate drop after config toggle in dev.",
        ],
        created_at=dt("2026-07-15T10:30:00"),
    ),
    IncidentRecord(
        incident_id="INC-DEMO0003",
        service="checkout-service",
        metric="dependency_timeout_ms",
        issue_type="dependency_failure",
        summary="[New] checkout-service - dependency_failure in prod",
        resolution="Adjusted upstream timeout and connection pool settings; downstream dependency stabilized.",
        runbook_actions=[
            "Run dependency health check and connection pool diagnostics.",
            "Update retry/backoff config in dev environment.",
            "Introduce circuit-breaker threshold adjustments in config.",
        ],
        created_at=dt("2026-07-18T14:05:00"),
    ),
]

changes = [
    ChangeRecord(
        change_id="CHG-DEMO0001",
        incident_id="INC-DEMO0001",
        service="payments-service",
        environment="prod",
        summary="[Recurring] payments-service - performance_degradation in prod",
        jira_key="SUP-1001",
        jenkins_job_url="https://jenkins.example.local/job/payments-service-dev-validation/4821/",
        proposed_actions=[
            "Check p95/p99 latency breakdown by endpoint in APM.",
            "Scale service replicas by +1 in dev and verify latency trend.",
            "Temporarily increase upstream timeout by 10-15% in dev config.",
        ],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=[
            "Metric=latency_p99_ms, value=1240.0, threshold=800.0",
            "Message signal: Checkout latency increased sharply after last deploy",
            "Environment=prod, service=payments-service",
        ],
        issue_type="performance_degradation",
        confidence=0.91,
        warning_count=0,
        jenkins_status="SUCCESS",
        pr_status="draft_created",
        pr_url="https://git.example.local/org/agentic-support/pull/8841",
        pr_branch="agent/chg-demo0001-payments-service",
        pr_title="[CHG-DEMO0001] [Recurring] payments-service - performance_degradation in prod",
        pr_summary="Issue type: performance_degradation (confidence=0.91)\nJira: SUP-1001\nPatch artifact: generated_patches/CHG-DEMO0001.patch",
        pr_generated_at=dt("2026-07-10T09:40:00"),
        pr_generated_by="approver-user",
        patch_artifact_path="generated_patches/CHG-DEMO0001.patch",
        patch_preview=(
            "# Change ID: CHG-DEMO0001\n"
            "# Service: payments-service\n"
            "diff --git a/config/service-overrides.yaml b/config/service-overrides.yaml\n"
            "+perf_tuning:\n"
            "+  connection_pool_max: 80\n"
            "+  request_timeout_ms: 2200\n"
        ),
        local_branch_created=False,
        local_branch_message="spec-only (git branch creation disabled)",
        code_change_status="not_started",
        code_change_message="spec-only (sandbox code change disabled)",
        sandbox_worktree_path="",
        changed_files=[],
        commit_sha="",
        push_status="not_attempted",
        test_evidence_status="passed",
        test_command="python -m pytest -q tests",
        test_output="Mock test evidence: simulated test pipeline passed.",
        test_pass_rate=1.0,
        test_run_at=dt("2026-07-10T09:41:00"),
        dev_execution_status="passed",
        dev_execution_url="https://jenkins.example.local/job/payments-service-dev-validation/4821/",
        dev_apm_improvement_pct=14.3,
        dev_smoke_tests_passed=True,
        dev_notes="dev execution: jenkins_status=SUCCESS; apm_improvement_pct=14.3; smoke_tests_passed=True; source_notes=mock-apm evidence for payments-service/CHG-DEMO0001",
        dev_executed_at=dt("2026-07-10T09:55:00"),
        dev_executed_by="approver-user",
        status="approved",
        deployment_state="promoted_to_prod",
        policy_reasons=[],
        created_at=dt("2026-07-10T09:12:00"),
        decided_at=dt("2026-07-10T10:05:00"),
        decided_by="approver-user",
        decision_comment="Confirmed fix resolves recurring latency issue.",
        promoted_at=dt("2026-07-10T10:40:00"),
        promoted_by="release-operator-user",
        promotion_comment="Promoting after successful dev validation.",
        promotion_result="success",
        prod_deploy_url="https://jenkins.example.local/job/payments-service-prod-deploy/5192/",
    ),
    ChangeRecord(
        change_id="CHG-DEMO0002",
        incident_id="INC-DEMO0002",
        service="orders-service",
        environment="prod",
        summary="[New] orders-service - application_error in prod",
        jira_key="SUP-1002",
        jenkins_job_url="https://jenkins.example.local/job/orders-service-dev-validation/4902/",
        proposed_actions=[
            "Inspect recent deployment diff and rollback candidate changes in dev.",
            "Enable safe-mode feature flags for failing module.",
            "Validate error-rate drop after config toggle in dev.",
        ],
        triage_mode_used="llm",
        triage_hypothesis_steps=[
            "Error rate spike began immediately after the 14:02 UTC deployment, consistent with a regression rather than infrastructure drift.",
            "5xx responses are concentrated on the /checkout/submit endpoint, suggesting a code-level fault in the new release.",
            "No corresponding CPU/memory pressure observed, ruling out resource saturation as the root cause.",
        ],
        issue_type="application_error",
        confidence=0.89,
        warning_count=0,
        jenkins_status="SUCCESS",
        pr_status="draft_created",
        pr_url="https://git.example.local/org/agentic-support/pull/8902",
        pr_branch="agent/chg-demo0002-orders-service",
        pr_title="[CHG-DEMO0002] [New] orders-service - application_error in prod",
        pr_summary="Issue type: application_error (confidence=0.89)\nJira: SUP-1002\nPatch artifact: generated_patches/CHG-DEMO0002.patch",
        pr_generated_at=dt("2026-07-20T13:10:00"),
        pr_generated_by="approver-user",
        patch_artifact_path="generated_patches/CHG-DEMO0002.patch",
        patch_preview=(
            "# Change ID: CHG-DEMO0002\n"
            "# Service: orders-service\n"
            "diff --git a/config/service-overrides.yaml b/config/service-overrides.yaml\n"
            "+error_guardrails:\n"
            "+  feature_flag_fallback: true\n"
            "+  retry:\n"
            "+    max_attempts: 2\n"
        ),
        local_branch_created=False,
        local_branch_message="spec-only (git branch creation disabled)",
        code_change_status="not_started",
        code_change_message="spec-only (sandbox code change disabled)",
        sandbox_worktree_path="",
        changed_files=[],
        commit_sha="",
        push_status="not_attempted",
        test_evidence_status="passed",
        test_command="python -m pytest -q tests",
        test_output="Mock test evidence: simulated test pipeline passed.",
        test_pass_rate=1.0,
        test_run_at=dt("2026-07-20T13:20:00"),
        dev_execution_status="passed",
        dev_execution_url="https://jenkins.example.local/job/orders-service-dev-validation/4902/",
        dev_apm_improvement_pct=11.2,
        dev_smoke_tests_passed=True,
        dev_notes="dev execution: jenkins_status=SUCCESS; apm_improvement_pct=11.2; smoke_tests_passed=True; source_notes=mock-apm evidence for orders-service/CHG-DEMO0002",
        dev_executed_at=dt("2026-07-20T13:20:00"),
        dev_executed_by="approver-user",
        status="pending_approval",
        deployment_state="ready_for_prod",
        policy_reasons=[],
        created_at=dt("2026-07-20T13:00:00"),
    ),
    ChangeRecord(
        change_id="CHG-DEMO0003",
        incident_id="INC-DEMO0004",
        service="checkout-gateway",
        environment="prod",
        summary="[New] checkout-gateway - unknown in prod",
        jira_key="SUP-1003",
        jenkins_job_url="https://jenkins.example.local/job/checkout-gateway-dev-validation/4930/",
        proposed_actions=[
            "Collect expanded logs and trace ids for the failing request path.",
            "Create manual triage task for SRE/on-call with service owner.",
        ],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=[
            "Metric=request_count, value=980.0, threshold=900.0",
            "Message signal: Unusual traffic pattern reported by synthetic monitor",
            "Environment=prod, service=checkout-gateway",
        ],
        issue_type="unknown",
        confidence=0.55,
        warning_count=0,
        jenkins_status="QUEUED",
        pr_status="not_started",
        code_change_status="not_started",
        test_evidence_status="not_run",
        dev_execution_status="not_started",
        status="pending_approval",
        deployment_state="awaiting_dev_execution",
        policy_reasons=[
            "confidence 0.55 below minimum 0.80",
            "issue_type unknown requires manual deep-dive",
            "dev execution evidence is not in passed state",
        ],
        created_at=dt("2026-07-21T08:15:00"),
    ),
    ChangeRecord(
        change_id="CHG-DEMO0004",
        incident_id="INC-DEMO0003",
        service="checkout-service",
        environment="prod",
        summary="[New] checkout-service - dependency_failure in prod",
        jira_key="SUP-1004",
        jenkins_job_url="https://jenkins.example.local/job/checkout-service-dev-validation/4885/",
        proposed_actions=[
            "Run dependency health check and connection pool diagnostics.",
            "Update retry/backoff config in dev environment.",
            "Introduce circuit-breaker threshold adjustments in config.",
        ],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=[
            "Metric=dependency_timeout_ms, value=3200.0, threshold=1500.0",
            "Message signal: Downstream inventory-db connection timeouts spiking",
            "Environment=prod, service=checkout-service",
        ],
        issue_type="dependency_failure",
        confidence=0.84,
        warning_count=1,
        jenkins_status="SUCCESS",
        pr_status="draft_created",
        pr_url="https://git.example.local/org/agentic-support/pull/8885",
        pr_branch="agent/chg-demo0004-checkout-service",
        pr_title="[CHG-DEMO0004] [New] checkout-service - dependency_failure in prod",
        pr_summary="Issue type: dependency_failure (confidence=0.84)\nJira: SUP-1004\nPatch artifact: generated_patches/CHG-DEMO0004.patch",
        pr_generated_at=dt("2026-07-19T11:20:00"),
        pr_generated_by="approver-user",
        patch_artifact_path="generated_patches/CHG-DEMO0004.patch",
        patch_preview=(
            "# Change ID: CHG-DEMO0004\n"
            "# Service: checkout-service\n"
            "diff --git a/config/service-overrides.yaml b/config/service-overrides.yaml\n"
            "+dependency_protection:\n"
            "+  upstream_timeout_ms: 1200\n"
            "+  fallback_response_mode: \"cached-or-default\"\n"
        ),
        local_branch_created=False,
        local_branch_message="spec-only (git branch creation disabled)",
        code_change_status="not_started",
        code_change_message="spec-only (sandbox code change disabled)",
        test_evidence_status="passed",
        test_command="python -m pytest -q tests",
        test_output="Mock test evidence: simulated test pipeline passed.",
        test_pass_rate=1.0,
        test_run_at=dt("2026-07-19T11:25:00"),
        dev_execution_status="passed",
        dev_execution_url="https://jenkins.example.local/job/checkout-service-dev-validation/4885/",
        dev_apm_improvement_pct=9.4,
        dev_smoke_tests_passed=True,
        dev_notes="dev execution: jenkins_status=SUCCESS; apm_improvement_pct=9.4; smoke_tests_passed=True; source_notes=mock-apm evidence for checkout-service/CHG-DEMO0004",
        dev_executed_at=dt("2026-07-19T11:15:00"),
        dev_executed_by="approver-user",
        status="approved",
        deployment_state="blocked_by_policy",
        policy_reasons=["warning_count 1 must be 0"],
        created_at=dt("2026-07-19T11:00:00"),
        decided_at=dt("2026-07-19T11:45:00"),
        decided_by="approver-user",
        decision_comment="Fix looks solid despite the integration warning.",
    ),
    ChangeRecord(
        change_id="CHG-DEMO0005",
        incident_id="INC-DEMO0005",
        service="cart-service",
        environment="prod",
        summary="[New] cart-service - application_error in prod",
        jira_key="SUP-1005",
        jenkins_job_url="https://jenkins.example.local/job/cart-service-dev-validation/4770/",
        proposed_actions=[
            "Inspect recent deployment diff and rollback candidate changes in dev.",
            "Enable safe-mode feature flags for failing module.",
            "Validate error-rate drop after config toggle in dev.",
        ],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=[
            "Metric=error_rate_pct, value=6.8, threshold=2.0",
            "Message signal: 5xx spike coincides with scheduled load test window",
            "Environment=prod, service=cart-service",
        ],
        issue_type="application_error",
        confidence=0.89,
        warning_count=0,
        jenkins_status="QUEUED",
        pr_status="not_started",
        code_change_status="not_started",
        test_evidence_status="not_run",
        dev_execution_status="not_started",
        status="rejected",
        deployment_state="closed_rejected",
        policy_reasons=[],
        created_at=dt("2026-07-17T15:40:00"),
        decided_at=dt("2026-07-17T16:00:00"),
        decided_by="approver-user",
        decision_comment="False positive - alert triggered by a scheduled load test, not real user traffic.",
    ),
]

data_dir = ROOT / "data"
(data_dir / "incident_history.json").write_text(
    json.dumps([i.model_dump(mode="json") for i in incidents], indent=2),
    encoding="utf-8",
)
(data_dir / "change_records.json").write_text(
    json.dumps([c.model_dump(mode="json") for c in changes], indent=2),
    encoding="utf-8",
)
(data_dir / "apm_alert_queue.json").write_text("[]", encoding="utf-8")
(data_dir / "processed_alerts.json").write_text("[]", encoding="utf-8")

print(f"Seeded {len(incidents)} incidents and {len(changes)} changes.")
