import json
from typing import Protocol
from urllib import request
from urllib.error import HTTPError, URLError
from uuid import uuid4

from app.models.schemas import PullRequestDraft


class PRClient(Protocol):
    def create_draft_pr(
        self,
        title: str,
        branch: str,
        body: str,
        base_branch: str = "main",
    ) -> PullRequestDraft:
        ...


class MockPRClient:
    def __init__(self, repo_slug: str = "org/agentic-support") -> None:
        self.repo_slug = repo_slug

    def create_draft_pr(
        self,
        title: str,
        branch: str,
        body: str,
        base_branch: str = "main",
    ) -> PullRequestDraft:
        _ = body
        _ = base_branch
        number = str(uuid4()).split("-")[0]
        return PullRequestDraft(
            title=title,
            branch=branch,
            url=f"https://git.example.local/{self.repo_slug}/pull/{number}",
            status="DRAFT",
        )


class RealGitHubPRClient:
    def __init__(self, repo_slug: str, token: str, api_base_url: str = "https://api.github.com") -> None:
        self.repo_slug = repo_slug.strip()
        self.token = token.strip()
        self.api_base_url = api_base_url.rstrip("/")
        if not self.repo_slug or not self.token:
            raise ValueError("GitHub PR config is incomplete for real integration")

    def create_draft_pr(
        self,
        title: str,
        branch: str,
        body: str,
        base_branch: str = "main",
    ) -> PullRequestDraft:
        url = f"{self.api_base_url}/repos/{self.repo_slug}/pulls"
        payload = {
            "title": title,
            "head": branch,
            "base": base_branch,
            "body": body,
            "draft": True,
        }
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"GitHub PR API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"GitHub PR connection error: {exc.reason}") from exc

        html_url = str(parsed.get("html_url", "")).strip()
        if not html_url:
            raise RuntimeError("GitHub PR response missing html_url")

        return PullRequestDraft(
            title=title,
            branch=branch,
            url=html_url,
            status="DRAFT",
        )
