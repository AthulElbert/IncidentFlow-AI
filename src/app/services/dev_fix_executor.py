from app.adapters.apm_client import APMClient
from app.adapters.jenkins_client import JenkinsClient
from app.models.schemas import ChangeRecord


class DevFixExecutor:
    def __init__(
        self,
        jenkins_client: JenkinsClient,
        apm_client: APMClient,
        min_apm_improvement_pct: float,
        require_smoke_tests: bool,
    ) -> None:
        self.jenkins = jenkins_client
        self.apm = apm_client
        self.min_apm_improvement_pct = min_apm_improvement_pct
        self.require_smoke_tests = require_smoke_tests

    def execute(self, change: ChangeRecord) -> tuple[str, str, float, bool, bool, str]:
        build = self.jenkins.trigger_dev_validation(service=change.service, issue_type=change.issue_type)
        jenkins_status = (build.status or "UNKNOWN").upper()

        evidence = self.apm.collect_dev_evidence(
            service=change.service,
            change_id=change.change_id,
            issue_type=change.issue_type,
        )

        smoke_ok = evidence.smoke_tests_passed
        if not self.require_smoke_tests:
            smoke_ok = True

        validation_passed = smoke_ok and evidence.apm_improvement_pct >= self.min_apm_improvement_pct

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
