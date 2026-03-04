import base64
import random
import ssl
from typing import Protocol
from urllib import parse, request
from urllib.error import HTTPError, URLError

from app.models.schemas import JenkinsBuildResult


class JenkinsClient(Protocol):
    def trigger_dev_validation(self, service: str, issue_type: str) -> JenkinsBuildResult:
        ...

    def trigger_prod_deploy(self, service: str, change_id: str) -> JenkinsBuildResult:
        ...


class MockJenkinsClient:
    def __init__(
        self,
        base_url: str = "https://jenkins.example.local",
        job_suffix: str = "-dev-validation",
        prod_job_suffix: str = "-prod-deploy",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.job_suffix = job_suffix
        self.prod_job_suffix = prod_job_suffix

    def trigger_dev_validation(self, service: str, issue_type: str) -> JenkinsBuildResult:
        build_number = random.randint(1000, 9999)
        job_name = f"{service}{self.job_suffix}"
        return JenkinsBuildResult(
            job_name=job_name,
            build_number=build_number,
            status="QUEUED",
            url=f"{self.base_url}/job/{job_name}/{build_number}/",
        )

    def trigger_prod_deploy(self, service: str, change_id: str) -> JenkinsBuildResult:
        build_number = random.randint(1000, 9999)
        job_name = f"{service}{self.prod_job_suffix}"
        return JenkinsBuildResult(
            job_name=job_name,
            build_number=build_number,
            status="QUEUED",
            url=f"{self.base_url}/job/{job_name}/{build_number}/",
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

    def _trigger_job(self, job_name: str, params: dict[str, str]) -> JenkinsBuildResult:
        endpoint = f"{self.base_url}/job/{parse.quote(job_name)}/buildWithParameters"
        query = parse.urlencode(params)
        url = f"{endpoint}?{query}"

        token = base64.b64encode(f"{self.user}:{self.api_token}".encode("utf-8")).decode("ascii")
        req = request.Request(
            url=url,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
            },
            method="POST",
        )

        context = None
        if not self.verify_ssl:
            context = ssl._create_unverified_context()

        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=context) as resp:
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
