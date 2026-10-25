from datetime import datetime, timezone

import pytest

from app.adapters.apm_client import APMEvidence
from app.adapters.jenkins_client import MockJenkinsClient
from app.models.schemas import ChangeRecord
from app.services.dev_fix_executor import DevFixExecutor


class _FixedAPM:
    def __init__(self, improvement=12.0, smoke=True):
        self.improvement = improvement
        self.smoke = smoke
        self.calls = 0

    def collect_dev_evidence(self, service, change_id, issue_type):
        self.calls += 1
        return APMEvidence(apm_improvement_pct=self.improvement, smoke_tests_passed=self.smoke, notes="fixed")


def _change(**overrides) -> ChangeRecord:
    params = dict(
        change_id="CHG-1",
        incident_id="INC-1",
        service="payments-service",
        environment="prod",
        summary="s",
        jira_key="SUP-1",
        jenkins_job_url="u",
        issue_type="performance_degradation",
        created_at=datetime.now(timezone.utc),
    )
    params.update(overrides)
    return ChangeRecord(**params)


def _executor(jenkins, apm, **kwargs) -> DevFixExecutor:
    return DevFixExecutor(
        jenkins_client=jenkins,
        apm_client=apm,
        min_apm_improvement_pct=5.0,
        require_smoke_tests=True,
        poll_interval_seconds=0,
        **kwargs,
    )


def test_passes_only_when_jenkins_succeeds_and_evidence_is_good():
    executor = _executor(MockJenkinsClient(polls_until_done=1), _FixedAPM(), build_wait_seconds=30)

    url, status, improvement, smoke_ok, passed, notes = executor.execute(_change())

    assert status == "SUCCESS"
    assert passed is True
    assert improvement == 12.0
    assert smoke_ok is True
    assert "jenkins_status=SUCCESS" in notes
    assert "/job/payments-service-dev-validation/" in url


def test_failed_jenkins_build_fails_validation_even_with_good_apm_evidence():
    executor = _executor(MockJenkinsClient(outcome="failure", polls_until_done=0), _FixedAPM(), build_wait_seconds=30)

    _, status, _, _, passed, _ = executor.execute(_change())

    assert status == "FAILURE"
    assert passed is False


def test_weak_apm_improvement_fails_validation():
    executor = _executor(MockJenkinsClient(polls_until_done=0), _FixedAPM(improvement=1.0), build_wait_seconds=30)

    assert executor.execute(_change())[4] is False


def test_unfinished_build_returns_no_verdict_and_skips_apm_evidence():
    apm = _FixedAPM()
    executor = _executor(MockJenkinsClient(polls_until_done=10_000), apm, build_wait_seconds=0)

    _, status, _, _, passed, notes = executor.execute(_change())

    assert status == "RUNNING"
    assert passed is None
    assert apm.calls == 0
    assert "still in progress" in notes


def test_resume_concludes_a_build_that_was_still_running():
    jenkins = MockJenkinsClient(polls_until_done=2)
    apm = _FixedAPM()
    executor = _executor(jenkins, apm, build_wait_seconds=0)

    url, status, _, _, passed, _ = executor.execute(_change())  # poll 1: RUNNING
    assert (status, passed) == ("RUNNING", None)

    running = _change(dev_execution_url=url, dev_execution_status="in_progress")
    assert executor.resume(running)[4] is None  # poll 2: RUNNING
    assert executor.resume(running)[4] is True  # poll 3: SUCCESS
    assert apm.calls == 1


def test_resume_requires_a_recorded_build():
    executor = _executor(MockJenkinsClient(), _FixedAPM())

    with pytest.raises(ValueError, match="No dev build"):
        executor.resume(_change())
