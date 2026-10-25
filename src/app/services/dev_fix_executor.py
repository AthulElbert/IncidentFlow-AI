from app.adapters.apm_client import APMClient
from app.adapters.jenkins_client import JenkinsClient, wait_for_build
from app.models.schemas import ChangeRecord, JenkinsBuildResult, is_non_terminal_build_status

# (execution_url, jenkins_status, apm_improvement_pct, smoke_ok, validation_passed, notes)
# validation_passed is None while the Jenkins build has not finished.
DevExecution = tuple[str, str, float, bool, "bool | None", str]


class DevFixExecutor:
    def __init__(
        self,
        jenkins_client: JenkinsClient,
        apm_client: APMClient,
        min_apm_improvement_pct: float,
        require_smoke_tests: bool,
        build_wait_seconds: float = 20.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.jenkins = jenkins_client
        self.apm = apm_client
        self.min_apm_improvement_pct = min_apm_improvement_pct
        self.require_smoke_tests = require_smoke_tests
        self.build_wait_seconds = build_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def execute(self, change: ChangeRecord) -> DevExecution:
        """Trigger the dev validation build and wait (bounded) for its verdict."""
        build = self.jenkins.trigger_dev_validation(service=change.service, issue_type=change.issue_type)
        build = wait_for_build(self.jenkins, build, self.build_wait_seconds, self.poll_interval_seconds)
        return self._conclude(change, build)

    def resume(self, change: ChangeRecord) -> DevExecution:
        """Poll a dev build that was still running when execute() gave up waiting."""
        if not change.dev_execution_url:
            raise ValueError("No dev build to refresh")
        placeholder = JenkinsBuildResult(
            job_name="",
            build_number=0,
            status="RUNNING",
            url=change.dev_execution_url,
        )
        build = wait_for_build(self.jenkins, placeholder, 0.0, self.poll_interval_seconds)
        return self._conclude(change, build)

    def _conclude(self, change: ChangeRecord, build: JenkinsBuildResult) -> DevExecution:
        jenkins_status = (build.status or "UNKNOWN").upper()

        if is_non_terminal_build_status(jenkins_status):
            notes = (
                f"dev execution: jenkins_status={jenkins_status}; "
                "build still in progress, refresh status later"
            )
            return build.url, jenkins_status, 0.0, False, None, notes

        evidence = self.apm.collect_dev_evidence(
            service=change.service,
            change_id=change.change_id,
            issue_type=change.issue_type,
        )

        smoke_ok = evidence.smoke_tests_passed
        if not self.require_smoke_tests:
            smoke_ok = True

        validation_passed = (
            jenkins_status == "SUCCESS"
            and smoke_ok
            and evidence.apm_improvement_pct >= self.min_apm_improvement_pct
        )

        notes = (
            f"dev execution: jenkins_status={jenkins_status}; "
            f"apm_improvement_pct={evidence.apm_improvement_pct}; "
            f"smoke_tests_passed={evidence.smoke_tests_passed}; "
            f"source_notes={evidence.notes}"
        )

        return (
            build.url,
            jenkins_status,
            evidence.apm_improvement_pct,
            evidence.smoke_tests_passed,
            validation_passed,
            notes,
        )
