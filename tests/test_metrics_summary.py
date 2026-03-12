from datetime import datetime, timedelta, timezone

from app.models.schemas import ChangeRecord
from app.services.metrics import build_metrics_summary


def _record(
    *,
    change_id: str,
    status: str,
    deployment_state: str,
    confidence: float,
    warning_count: int,
    issue_type: str,
    triage_mode_used: str,
    dev_execution_status: str = "not_started",
    dev_offset_sec: int | None = None,
    prod_offset_sec: int | None = None,
) -> ChangeRecord:
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    dev_at = (base + timedelta(seconds=dev_offset_sec)) if dev_offset_sec is not None else None
    prod_at = (base + timedelta(seconds=prod_offset_sec)) if prod_offset_sec is not None else None
    return ChangeRecord(
        change_id=change_id,
        incident_id=f"INC-{change_id}",
        service="payments-service",
        environment="prod",
        summary="x",
        jira_key="SUP-1",
        jenkins_job_url="http://jenkins/job/1",
        proposed_actions=["a1"],
        triage_mode_used=triage_mode_used,
        triage_hypothesis_steps=["h1"],
        issue_type=issue_type,
        confidence=confidence,
        warning_count=warning_count,
        jenkins_status="QUEUED",
        dev_execution_status=dev_execution_status,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        deployment_state=deployment_state,  # type: ignore[arg-type]
        created_at=base,
        dev_executed_at=dev_at,
        promoted_at=prod_at,
    )


def test_build_metrics_summary_calculates_expected_rates():
    changes = [
        _record(
            change_id="CHG-1",
            status="approved",
            deployment_state="promoted_to_prod",
            confidence=0.95,
            warning_count=0,
            issue_type="performance_degradation",
            triage_mode_used="llm",
            dev_execution_status="passed",
            dev_offset_sec=60,
            prod_offset_sec=300,
        ),
        _record(
            change_id="CHG-2",
            status="approved",
            deployment_state="blocked_by_policy",
            confidence=0.55,
            warning_count=2,
            issue_type="unknown",
            triage_mode_used="heuristic",
            dev_execution_status="failed",
            dev_offset_sec=120,
        ),
    ]

    summary = build_metrics_summary(changes, min_confidence_for_prod=0.80)

    assert summary.total_changes == 2
    assert summary.warning_count_total == 2
    assert summary.warning_rate == 0.5
    assert summary.policy_block_rate == 0.5
    assert summary.low_confidence_count == 1
    assert summary.dev_success_rate == 0.5
    assert summary.promotion_success_rate == 0.5
    assert summary.avg_time_to_dev_seconds == 90.0
    assert summary.avg_time_to_prod_seconds == 300.0
