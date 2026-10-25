import json

import pytest

from app.models.schemas import ApprovalDecisionRequest, PromoteRequest
from app.services.change_control import ChangeControlStore, PolicyConfig


def _store(tmp_path, **policy_kwargs) -> ChangeControlStore:
    store_file = tmp_path / "change_records.json"
    store_file.write_text(json.dumps([]), encoding="utf-8")
    policy = PolicyConfig(**{"min_confidence_for_prod": 0.8, **policy_kwargs})
    return ChangeControlStore(str(store_file), policy=policy)


def _new_change(store, **overrides):
    params = dict(
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
    params.update(overrides)
    return store.create_change(**params)


def _run_dev(store, change_id, *, jenkins_status="SUCCESS", validation_passed=True):
    return store.record_dev_execution(
        change_id=change_id,
        executed_by="approver-1",
        execution_url="https://jenkins/job/payments-service-dev-validation/11/",
        jenkins_status=jenkins_status,
        apm_improvement_pct=12.5,
        smoke_tests_passed=True,
        validation_passed=validation_passed,
        notes="dev run",
    )


def _approved_change(store, **overrides):
    change = _new_change(store, **overrides)
    _run_dev(store, change.change_id)
    return store.apply_decision(
        change_id=change.change_id,
        payload=ApprovalDecisionRequest(decision="approve", comment="Looks good"),
        decided_by="approver-1",
    )


def test_full_lifecycle_requires_confirmed_success_to_promote(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store)

    executed = _run_dev(store, change.change_id)
    assert executed.deployment_state == "ready_for_prod"
    assert executed.dev_execution_status == "passed"

    approved = store.apply_decision(
        change_id=change.change_id,
        payload=ApprovalDecisionRequest(decision="approve", comment="Looks good"),
        decided_by="approver-1",
    )
    assert approved.deployment_state == "ready_for_prod"
    assert approved.decided_by == "approver-1"
    assert approved.policy_reasons == []

    promoting = store.begin_promotion(change.change_id, PromoteRequest(comment="go"), promoted_by="release-op-1")
    assert promoting.deployment_state == "promoting"
    assert promoting.promotion_key

    done = store.finalize_promotion(change.change_id, "success", "https://jenkins/job/payments-service-prod-deploy/101/")
    assert done.deployment_state == "promoted_to_prod"
    assert done.promotion_result == "success"
    assert done.promoted_by == "release-op-1"
    assert done.promoted_at is not None


def test_queued_prod_deploy_is_not_recorded_as_promoted(tmp_path):
    store = _store(tmp_path)
    approved = _approved_change(store)
    store.begin_promotion(approved.change_id, PromoteRequest(), promoted_by="release-op-1")

    still_running = store.finalize_promotion(approved.change_id, "queued", "https://jenkins/queue/item/9/")
    assert still_running.deployment_state == "promoting"
    assert still_running.promotion_result == "queued"
    assert still_running.promoted_at is None

    done = store.finalize_promotion(approved.change_id, "success", "https://jenkins/job/x/9/")
    assert done.deployment_state == "promoted_to_prod"


def test_failed_prod_deploy_is_recorded_and_not_retryable(tmp_path):
    store = _store(tmp_path)
    approved = _approved_change(store)
    store.begin_promotion(approved.change_id, PromoteRequest(), promoted_by="release-op-1")

    failed = store.finalize_promotion(approved.change_id, "failed", "", note="deploy trigger failed")
    assert failed.deployment_state == "prod_promotion_failed"
    assert failed.promoted_at is None
    assert "deploy trigger failed" in (failed.promotion_comment or "")

    with pytest.raises(ValueError, match="already promoted"):
        store.begin_promotion(approved.change_id, PromoteRequest(), promoted_by="release-op-2")


def test_second_promotion_attempt_is_rejected_while_promoting(tmp_path):
    store = _store(tmp_path)
    approved = _approved_change(store)
    store.begin_promotion(approved.change_id, PromoteRequest(), promoted_by="release-op-1")

    with pytest.raises(ValueError, match="already being promoted"):
        store.begin_promotion(approved.change_id, PromoteRequest(), promoted_by="release-op-2")


def test_promotion_requires_a_different_actor_than_the_approver(tmp_path):
    store = _store(tmp_path)
    approved = _approved_change(store)

    with pytest.raises(ValueError, match="different actor"):
        store.begin_promotion(approved.change_id, PromoteRequest(), promoted_by="approver-1")
    assert store.get_change(approved.change_id).deployment_state == "ready_for_prod"


def test_separation_of_duties_can_be_disabled_for_local_demos(tmp_path):
    store = _store(tmp_path, enforce_separation_of_duties=False)
    approved = _approved_change(store)

    promoting = store.begin_promotion(approved.change_id, PromoteRequest(), promoted_by="approver-1")
    assert promoting.deployment_state == "promoting"


def test_promotion_requires_approval(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store)
    _run_dev(store, change.change_id)

    with pytest.raises(ValueError, match="must be approved"):
        store.begin_promotion(change.change_id, PromoteRequest(), promoted_by="release-op-1")
    assert store.get_change(change.change_id).deployment_state == "ready_for_prod"


def test_finalize_requires_a_reserved_promotion(tmp_path):
    store = _store(tmp_path)
    approved = _approved_change(store)

    with pytest.raises(ValueError, match="not in promoting state"):
        store.finalize_promotion(approved.change_id, "success", "https://jenkins/job/x/1/")


def test_queued_dev_build_does_not_satisfy_policy(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store)
    _run_dev(store, change.change_id, jenkins_status="QUEUED")

    blocked = store.apply_decision(
        change_id=change.change_id,
        payload=ApprovalDecisionRequest(decision="approve", comment="ok"),
        decided_by="approver-1",
    )
    assert blocked.deployment_state == "blocked_by_policy"
    assert any("jenkins_status QUEUED" in reason for reason in blocked.policy_reasons)

    with pytest.raises(ValueError, match="not ready_for_prod"):
        store.begin_promotion(change.change_id, PromoteRequest(), promoted_by="release-op-1")


def test_dev_build_still_running_is_recorded_as_in_progress(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store)

    running = _run_dev(store, change.change_id, jenkins_status="RUNNING", validation_passed=None)
    assert running.dev_execution_status == "in_progress"
    assert running.deployment_state == "dev_fix_in_progress"

    with pytest.raises(ValueError, match="not ready for approval"):
        store.apply_decision(
            change_id=change.change_id,
            payload=ApprovalDecisionRequest(decision="approve", comment="too early"),
            decided_by="approver-1",
        )

    finished = _run_dev(store, change.change_id, jenkins_status="SUCCESS", validation_passed=True)
    assert finished.deployment_state == "ready_for_prod"


def test_ensure_dev_executable_blocks_duplicate_and_finished_runs(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store)
    assert store.ensure_dev_executable(change.change_id).change_id == change.change_id

    _run_dev(store, change.change_id, jenkins_status="RUNNING", validation_passed=None)
    with pytest.raises(ValueError, match="already in progress"):
        store.ensure_dev_executable(change.change_id)

    finished = _approved_change(store, incident_id="INC-2")
    with pytest.raises(ValueError, match="already finalized"):
        store.ensure_dev_executable(finished.change_id)

    with pytest.raises(ValueError, match="not found"):
        store.ensure_dev_executable("CHG-NOPE")


def test_policy_config_rejects_non_terminal_jenkins_states():
    with pytest.raises(ValueError, match="non-terminal"):
        PolicyConfig(allowed_jenkins_states_for_prod=["SUCCESS", "QUEUED"])
    with pytest.raises(ValueError, match="non-terminal"):
        PolicyConfig(allowed_jenkins_states_for_prod=["unknown"])


def test_policy_blocks_prod_readiness(tmp_path):
    store = _store(tmp_path, min_confidence_for_prod=0.9)
    change = _new_change(
        store,
        incident_id="INC-3333",
        summary="Low confidence mitigation",
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
        store.begin_promotion(change.change_id, PromoteRequest(comment="force"), promoted_by="release-op-1")


def test_reject_and_prevent_second_decision(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store, incident_id="INC-2222", issue_type="application_error", confidence=0.89)

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
        store.begin_promotion(change.change_id, PromoteRequest(comment="should fail"), promoted_by="release-op-1")


def test_every_write_bumps_the_record_version_and_persists(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store)
    assert change.version == 1

    after_dev = _run_dev(store, change.change_id)
    assert after_dev.version == 2

    reloaded = ChangeControlStore(str(tmp_path / "change_records.json"))
    assert reloaded.get_change(change.change_id).version == 2


def test_json_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    store = _store(tmp_path)
    change = _new_change(store)
    _run_dev(store, change.change_id)

    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads((tmp_path / "change_records.json").read_text(encoding="utf-8"))[0]["version"] == 2
