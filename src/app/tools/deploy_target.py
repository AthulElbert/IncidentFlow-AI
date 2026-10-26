import threading
from typing import Protocol

from app.adapters.jenkins_client import JenkinsClient
from app.tools.base import ToolUnavailableError
from app.tools.models import DeployResult


class DeployTarget(Protocol):
    def deploy_dev(self, service: str, ref: str, issue_type: str = "unknown") -> DeployResult:
        """Deploy `ref` to the dev environment. Never touches production."""
        ...

    def get_status(self, deploy_url: str) -> DeployResult:
        ...

    def canary(self, service: str, ref: str, percent: int) -> DeployResult:
        ...

    def rollback(self, service: str, ref: str) -> DeployResult:
        ...


class MockDeployTarget:
    """Simulated environment: a deploy reports RUNNING for N polls, then the configured outcome."""

    def __init__(self, outcome: str = "success", polls_until_done: int = 1) -> None:
        self.outcome = "SUCCESS" if outcome.strip().lower() in {"success", "ok", "passed"} else "FAILURE"
        self.polls_until_done = max(0, polls_until_done)
        self._counter = 0
        self._polls: dict[str, int] = {}
        self._lock = threading.Lock()

    def _start(self, kind: str, service: str, ref: str) -> DeployResult:
        with self._lock:
            self._counter += 1
            number = self._counter
        return DeployResult(
            status="QUEUED",
            url=f"mock://deploy/{kind}/{service}/{number}",
            detail=f"{kind} of {ref} for {service}",
        )

    def deploy_dev(self, service: str, ref: str, issue_type: str = "unknown") -> DeployResult:
        return self._start("dev", service, ref)

    def get_status(self, deploy_url: str) -> DeployResult:
        with self._lock:
            polls = self._polls.get(deploy_url, 0) + 1
            self._polls[deploy_url] = polls
        status = "RUNNING" if polls <= self.polls_until_done else self.outcome
        return DeployResult(status=status, url=deploy_url)

    def canary(self, service: str, ref: str, percent: int) -> DeployResult:
        return self._start(f"canary-{percent}", service, ref)

    def rollback(self, service: str, ref: str) -> DeployResult:
        return self._start("rollback", service, ref)


class JenkinsDeployTarget:
    """Dev deploys through the existing Jenkins dev-validation job.

    Canary and rollback are not available here: they are production operations that
    belong to the governed promotion flow, not to a tool an agent can call.
    """

    def __init__(self, jenkins_client: JenkinsClient) -> None:
        self.jenkins = jenkins_client

    def deploy_dev(self, service: str, ref: str, issue_type: str = "unknown") -> DeployResult:
        build = self.jenkins.trigger_dev_validation(service=service, issue_type=issue_type)
        return DeployResult(status=(build.status or "UNKNOWN").upper(), url=build.url, detail=f"jenkins job {build.job_name}")

    def get_status(self, deploy_url: str) -> DeployResult:
        build = self.jenkins.get_build_status(deploy_url)
        return DeployResult(status=(build.status or "UNKNOWN").upper(), url=build.url, detail=build.job_name)

    def canary(self, service: str, ref: str, percent: int) -> DeployResult:
        raise ToolUnavailableError("canary deploys are not available through the Jenkins deploy target")

    def rollback(self, service: str, ref: str) -> DeployResult:
        raise ToolUnavailableError("rollback is not available through the Jenkins deploy target")
