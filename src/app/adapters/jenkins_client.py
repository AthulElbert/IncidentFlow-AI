import base64
import json
import random
import re
import ssl
import time
from threading import Lock
from typing import Protocol
from urllib import parse, request
from urllib.error import HTTPError, URLError

from app.models.schemas import JenkinsBuildResult, is_non_terminal_build_status

_RESULT_MAP = {
    "SUCCESS": "SUCCESS",
    "FAILURE": "FAILURE",
    "UNSTABLE": "UNSTABLE",
    "ABORTED": "ABORTED",
    "NOT_BUILT": "ABORTED",
}


class JenkinsClient(Protocol):
    def trigger_dev_validation(self, service: str, issue_type: str) -> JenkinsBuildResult:
        ...

    def trigger_prod_deploy(self, service: str, change_id: str) -> JenkinsBuildResult:
        ...

    def get_build_status(self, build_url: str) -> JenkinsBuildResult:
        """Poll a previously triggered build. The returned url is the resolved build url."""
        ...


def wait_for_build(
    jenkins: JenkinsClient,
    build: JenkinsBuildResult,
    wait_seconds: float,
    poll_interval_seconds: float,
) -> JenkinsBuildResult:
    """Poll until the build reaches a terminal state or the wait budget runs out.

    Always polls at least once when the build is non-terminal. A build that is
    still running when the budget expires is returned as-is (non-terminal).
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    current = build
    while is_non_terminal_build_status(current.status):
        current = jenkins.get_build_status(current.url)
        if not is_non_terminal_build_status(current.status):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(max(poll_interval_seconds, 0.01))
    return current


class MockJenkinsClient:
    """Deterministic stand-in: a build reports RUNNING for N polls, then the configured outcome."""

    def __init__(
        self,
        base_url: str = "https://jenkins.example.local",
        job_suffix: str = "-dev-validation",
        prod_job_suffix: str = "-prod-deploy",
        outcome: str = "success",
        polls_until_done: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.job_suffix = job_suffix
        self.prod_job_suffix = prod_job_suffix
        self.outcome = "SUCCESS" if outcome.strip().lower() in {"success", "ok", "passed"} else "FAILURE"
        self.polls_until_done = max(0, polls_until_done)
        self._poll_counts: dict[str, int] = {}
        self._lock = Lock()

    def _queue(self, job_name: str) -> JenkinsBuildResult:
        build_number = random.randint(1000, 9999)
        return JenkinsBuildResult(
            job_name=job_name,
            build_number=build_number,
            status="QUEUED",
            url=f"{self.base_url}/job/{job_name}/{build_number}/",
        )

    def trigger_dev_validation(self, service: str, issue_type: str) -> JenkinsBuildResult:
        return self._queue(f"{service}{self.job_suffix}")

    def trigger_prod_deploy(self, service: str, change_id: str) -> JenkinsBuildResult:
        return self._queue(f"{service}{self.prod_job_suffix}")

    def get_build_status(self, build_url: str) -> JenkinsBuildResult:
        with self._lock:
            polls = self._poll_counts.get(build_url, 0) + 1
            self._poll_counts[build_url] = polls
        status = "RUNNING" if polls <= self.polls_until_done else self.outcome
        match = re.search(r"/job/([^/]+)/(\d+)/?$", build_url)
        return JenkinsBuildResult(
            job_name=match.group(1) if match else "",
            build_number=int(match.group(2)) if match else 0,
            status=status,
            url=build_url,
        )


class RealJenkinsClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        api_token: str,
        job_suffix: str = "-dev-validation",
        prod_job_suffix: str = "-prod-deploy",
        verify_ssl: bool = True,
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.api_token = api_token
        self.job_suffix = job_suffix
        self.prod_job_suffix = prod_job_suffix
        self.verify_ssl = verify_ssl
        self.timeout_seconds = timeout_seconds

        if not all([self.base_url, self.user, self.api_token]):
            raise ValueError("Jenkins config is incomplete for real integration")

    def _auth_headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.user}:{self.api_token}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _ssl_context(self):
        return None if self.verify_ssl else ssl._create_unverified_context()

    def _trigger_job(self, job_name: str, params: dict[str, str]) -> JenkinsBuildResult:
        endpoint = f"{self.base_url}/job/{parse.quote(job_name)}/buildWithParameters"
        query = parse.urlencode(params)
        url = f"{endpoint}?{query}"

        req = request.Request(url=url, headers=self._auth_headers(), method="POST")

        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=self._ssl_context()) as resp:
                queue_url = resp.headers.get("Location", "")
                status_code = resp.status
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"Jenkins API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Jenkins connection error: {exc.reason}") from exc

        return JenkinsBuildResult(
            job_name=job_name,
            build_number=0,
            status="QUEUED" if status_code in {200, 201, 202} else "UNKNOWN",
            url=queue_url or f"{self.base_url}/job/{job_name}/",
        )

    def _get_json(self, url: str) -> dict:
        req = request.Request(url=url, headers=self._auth_headers(), method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=self._ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"Jenkins API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Jenkins connection error: {exc.reason}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Jenkins returned invalid JSON from {url}") from exc

    def get_build_status(self, build_url: str) -> JenkinsBuildResult:
        resolved_url = build_url
        data = self._get_json(build_url.rstrip("/") + "/api/json")

        if "/queue/item/" in build_url:
            if data.get("cancelled"):
                return JenkinsBuildResult(job_name="", build_number=0, status="ABORTED", url=build_url)
            executable = data.get("executable")
            if not executable or not executable.get("url"):
                return JenkinsBuildResult(job_name="", build_number=0, status="QUEUED", url=build_url)
            # The queue item has become a build; from now on track the build url itself,
            # because Jenkins drops queue items shortly after they start.
            resolved_url = executable["url"]
            data = self._get_json(resolved_url.rstrip("/") + "/api/json")

        building = bool(data.get("building"))
        result = data.get("result")
        if building or result is None:
            status = "RUNNING"
        else:
            status = _RESULT_MAP.get(str(result).upper(), "UNKNOWN")

        return JenkinsBuildResult(
            job_name=str(data.get("fullDisplayName") or data.get("displayName") or ""),
            build_number=int(data.get("number") or 0),
            status=status,
            url=str(data.get("url") or resolved_url),
        )

    def trigger_dev_validation(self, service: str, issue_type: str) -> JenkinsBuildResult:
        return self._trigger_job(
            job_name=f"{service}{self.job_suffix}",
            params={"SERVICE": service, "ISSUE_TYPE": issue_type},
        )

    def trigger_prod_deploy(self, service: str, change_id: str) -> JenkinsBuildResult:
        return self._trigger_job(
            job_name=f"{service}{self.prod_job_suffix}",
            params={"SERVICE": service, "CHANGE_ID": change_id, "TARGET_ENV": "prod"},
        )
