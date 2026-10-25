from app.adapters.jenkins_client import JenkinsClient
from app.models.schemas import ChangeRecord, JenkinsBuildResult, is_non_terminal_build_status
from app.services.change_control import ChangeControlStore
from app.services.dev_fix_executor import DevFixExecutor


def promotion_result_for_status(status: str | None) -> str:
    """Map a Jenkins prod-deploy status to a promotion result.

    Only SUCCESS counts as success; a build that has not finished is `queued`.
    """
    normalized = (status or "UNKNOWN").upper()
    if normalized == "SUCCESS":
        return "success"
    if is_non_terminal_build_status(normalized):
        return "queued"
    return "failed"


class BuildTracker:
    """Resolves Jenkins builds that were still running when the original request returned."""

    def __init__(
        self,
        change_store: ChangeControlStore,
        jenkins_client: JenkinsClient,
        dev_executor: DevFixExecutor,
    ) -> None:
        self.change_store = change_store
        self.jenkins = jenkins_client
        self.dev_executor = dev_executor

    def refresh(self, change_id: str, actor: str) -> ChangeRecord:
        record = self.change_store.get_change(change_id)
        if not record:
            raise ValueError("Change not found")

        if record.deployment_state == "promoting":
            return self._refresh_promotion(record)
        if record.deployment_state == "dev_fix_in_progress":
            return self._refresh_dev(record, actor)
        raise ValueError("No build in progress for this change")

    def _refresh_promotion(self, record: ChangeRecord) -> ChangeRecord:
        if not record.prod_deploy_url:
            raise ValueError("No prod deploy build to refresh")
        build: JenkinsBuildResult = self.jenkins.get_build_status(record.prod_deploy_url)
        return self.change_store.finalize_promotion(
            change_id=record.change_id,
            promotion_result=promotion_result_for_status(build.status),
            prod_deploy_url=build.url,
        )

    def _refresh_dev(self, record: ChangeRecord, actor: str) -> ChangeRecord:
        url, jenkins_status, apm_improvement, smoke_ok, validation_passed, notes = self.dev_executor.resume(record)
        return self.change_store.record_dev_execution(
            change_id=record.change_id,
            executed_by=actor,
            execution_url=url,
            jenkins_status=jenkins_status,
            apm_improvement_pct=apm_improvement,
            smoke_tests_passed=smoke_ok,
            validation_passed=validation_passed,
            notes=notes,
        )
