import json

import pytest

from app.models.schemas import ApprovalDecisionRequest, PromoteRequest
from app.services.change_control import ChangeControlStore, PolicyConfig


def test_change_control_create_approve_and_promote(tmp_path):
    store_file = tmp_path / "change_records.json"
    store_file.write_text(json.dumps([]), encoding="utf-8")

    store = ChangeControlStore(
        str(store_file),
        policy=PolicyConfig(min_confidence_for_prod=0.8, require_zero_warnings_for_prod=True),
    )
    change = store.create_change(
        incident_id="INC-1111",
        service="payments-service",
        environment="prod",
        summary="Latency fix proposal",
        jira_key="SUP-123",
        jenkins_job_url="https://jenkins/job/payments-service-dev-validation/1/",
        proposed_actions=["Scale replicas", "Tune timeout"],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=["step1"],
        issue_type="performance_degradation",
        confidence=0.91,
        warning_count=0,
        jenkins_status="QUEUED",
    )
    executed = store.record_dev_execution(
        change_id=change.change_id,
        executed_by="approver-1",
        execution_url="https://jenkins/job/payments-service-dev-validation/11/",
        jenkins_status="QUEUED",
        apm_improvement_pct=12.5,
        smoke_tests_passed=True,
        validation_passed=True,
        notes="dev run passed",
    )
    assert executed.deployment_state == "ready_for_prod"
    assert executed.dev_execution_status == "passed"

    approved = store.apply_decision(
        change_id=change.change_id,
        payload=ApprovalDecisionRequest(decision="approve", comment="Looks good"),
        decided_by="approver-1",
    )
    assert approved.deployment_state == "ready_for_prod"
    assert approved.decided_by == "approver-1"

    promoted = store.promote_change(
        change_id=change.change_id,
        payload=PromoteRequest(comment="prod deploy approved"),
        promoted_by="release-op-1",
        promotion_result="queued",
        prod_deploy_url="https://jenkins/job/payments-service-prod-deploy/101/",
    )
    assert promoted.deployment_state == "promoted_to_prod"
    assert promoted.promotion_result == "queued"
    assert promoted.promoted_by == "release-op-1"


def test_change_control_policy_blocks_prod_readiness(tmp_path):
    store_file = tmp_path / "change_records.json"
    store_file.write_text(json.dumps([]), encoding="utf-8")

    store = ChangeControlStore(
        str(store_file),
        policy=PolicyConfig(min_confidence_for_prod=0.9, require_zero_warnings_for_prod=True),
    )
    change = store.create_change(
        incident_id="INC-3333",
        service="payments-service",
        environment="prod",
        summary="Low confidence mitigation",
        jira_key="SUP-333",
        jenkins_job_url="https://jenkins/job/payments-service-dev-validation/3/",
        proposed_actions=["Scale replicas"],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=["step1"],
        issue_type="unknown",
        confidence=0.55,
        warning_count=1,
        jenkins_status="FAILED",
    )
    executed = store.record_dev_execution(
        change_id=change.change_id,
        executed_by="approver-1",
        execution_url="https://jenkins/job/payments-service-dev-validation/33/",
        jenkins_status="FAILED",
        apm_improvement_pct=2.0,
        smoke_tests_passed=False,
        validation_passed=False,
        notes="dev run failed",
    )
    assert executed.deployment_state == "dev_fix_failed"
    assert executed.dev_execution_status == "failed"

    with pytest.raises(ValueError, match="execute dev fix first"):
        store.apply_decision(
            change_id=change.change_id,
            payload=ApprovalDecisionRequest(decision="approve", comment="manual approve"),
            decided_by="approver-1",
        )

    with pytest.raises(ValueError, match="must be approved"):
        store.promote_change(
            change_id=change.change_id,
            payload=PromoteRequest(comment="force"),
            promoted_by="release-op-1",
            promotion_result="queued",
            prod_deploy_url="https://jenkins/job/x/1/",
        )


def test_change_control_reject_and_prevent_second_decision(tmp_path):
    store_file = tmp_path / "change_records.json"
    store_file.write_text(json.dumps([]), encoding="utf-8")

    store = ChangeControlStore(str(store_file))
    change = store.create_change(
        incident_id="INC-2222",
        service="orders-service",
        environment="prod",
        summary="Error rate mitigation",
        jira_key="SUP-456",
        jenkins_job_url="https://jenkins/job/orders-service-dev-validation/2/",
        proposed_actions=["Disable feature flag"],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=["step1"],
        issue_type="application_error",
        confidence=0.89,
        warning_count=0,
        jenkins_status="QUEUED",
    )

    rejected = store.apply_decision(
        change_id=change.change_id,
        payload=ApprovalDecisionRequest(decision="reject", comment="Need more evidence"),
        decided_by="approver-1",
    )
    assert rejected.status == "rejected"
    assert rejected.deployment_state == "closed_rejected"

    with pytest.raises(ValueError, match="already decided"):
        store.apply_decision(
            change_id=change.change_id,
            payload=ApprovalDecisionRequest(decision="approve", comment="retry"),
            decided_by="approver-1",
        )

    with pytest.raises(ValueError, match="must be approved"):
        store.promote_change(
            change_id=change.change_id,
            payload=PromoteRequest(comment="should fail"),
            promoted_by="release-op-1",
            promotion_result="queued",
            prod_deploy_url="https://jenkins/job/x/2/",
        )
