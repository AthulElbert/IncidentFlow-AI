from pathlib import Path

from app.adapters.apm_client import HttpAPMClient, MockAPMClient
from app.adapters.apm_alert_source import DynatraceAPMAlertSource, HttpAPMAlertSource, MockAPMAlertSource
from app.adapters.jenkins_client import MockJenkinsClient, RealJenkinsClient
from app.adapters.jira_client import MockJiraClient, RealJiraClient
from app.adapters.llm_client import OpenAICompatibleLLMClient
from app.adapters.pr_client import MockPRClient, RealGitHubPRClient
from app.config import Settings


class IntegrationConfigError(ValueError):
    """A client configured for a real backend could not be started."""


def raise_unless_mock_fallback(settings: Settings, component: str, exc: Exception) -> None:
    """Real mode must not quietly degrade to a simulator.

    ALLOW_MOCK_FALLBACK=true is the explicit, demo-only opt-in.
    """
    if settings.allow_mock_fallback:
        return
    raise IntegrationConfigError(
        f"{component} is configured for a real backend but could not start: {exc}. "
        "Fix the configuration, or set ALLOW_MOCK_FALLBACK=true to run against a simulator (demo only)."
    ) from exc


def _mock_jenkins(settings: Settings) -> MockJenkinsClient:
    return MockJenkinsClient(
        base_url=settings.jenkins_base_url,
        job_suffix=settings.jenkins_job_suffix,
        prod_job_suffix=settings.jenkins_prod_job_suffix,
        outcome=settings.mock_jenkins_outcome,
        polls_until_done=settings.mock_jenkins_polls_until_done,
    )


def build_jira_client(settings: Settings):
    if settings.jira_mode == "real":
        try:
            return RealJiraClient(
                base_url=settings.jira_base_url,
                project_key=settings.jira_project_key,
                email=settings.jira_email,
                api_token=settings.jira_api_token,
            ), "real"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "Jira", exc)
            return MockJiraClient(project_key=settings.jira_project_key), "mock-fallback-startup"

    return MockJiraClient(project_key=settings.jira_project_key), "mock"


def build_jenkins_client(settings: Settings):
    if settings.jenkins_mode == "real":
        try:
            return RealJenkinsClient(
                base_url=settings.jenkins_base_url,
                user=settings.jenkins_user,
                api_token=settings.jenkins_api_token,
                job_suffix=settings.jenkins_job_suffix,
                prod_job_suffix=settings.jenkins_prod_job_suffix,
                verify_ssl=settings.jenkins_verify_ssl,
            ), "real"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "Jenkins", exc)
            return _mock_jenkins(settings), "mock-fallback-startup"

    return _mock_jenkins(settings), "mock"


def build_apm_client(settings: Settings):
    if settings.apm_mode == "http":
        try:
            return HttpAPMClient(
                base_url=settings.apm_base_url,
                timeout_seconds=settings.apm_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            ), "http"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "APM evidence client", exc)
            return MockAPMClient(), "mock-fallback-startup"

    return MockAPMClient(), "mock"


def build_apm_alert_source(settings: Settings, base_dir: str):
    queue_path = str(Path(base_dir) / settings.apm_alert_queue_file)

    if settings.apm_alerts_mode == "dynatrace":
        try:
            return DynatraceAPMAlertSource(
                base_url=settings.apm_alerts_base_url,
                api_token=settings.apm_alerts_dynatrace_token,
                timeout_seconds=settings.apm_alerts_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            ), "dynatrace"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "Dynatrace alert source", exc)
            return MockAPMAlertSource(queue_file=queue_path), "mock-fallback-startup"

    if settings.apm_alerts_mode == "http":
        try:
            return HttpAPMAlertSource(
                base_url=settings.apm_alerts_base_url,
                timeout_seconds=settings.apm_alerts_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            ), "http"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "HTTP alert source", exc)
            return MockAPMAlertSource(queue_file=queue_path), "mock-fallback-startup"

    return MockAPMAlertSource(queue_file=queue_path), "mock"


def build_llm_client(settings: Settings):
    """Build the LLM client when any LLM-backed feature (triage or code-fix) needs it.

    The returned mode describes triage only: "llm" if triage uses the model,
    otherwise "heuristic" even when the client exists for code-fix.
    """
    needs_llm = (
        settings.triage_mode == "llm" or settings.code_fix_mode == "llm" or settings.investigator_mode == "llm"
    )
    if not needs_llm:
        return None, "heuristic"

    try:
        client = OpenAICompatibleLLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            verify_ssl=settings.llm_verify_ssl,
        )
    except Exception as exc:
        raise_unless_mock_fallback(settings, "LLM client", exc)
        return None, "heuristic-fallback-startup"

    return client, ("llm" if settings.triage_mode == "llm" else "heuristic")


def build_investigator_llm(settings: Settings, shared_client):
    """The Investigator may use its own model (INVESTIGATOR_MODEL); otherwise it shares the main client."""
    if settings.investigator_mode != "llm":
        return None
    if not settings.investigator_model or settings.investigator_model == settings.llm_model:
        return shared_client
    try:
        return OpenAICompatibleLLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.investigator_model,
            timeout_seconds=settings.llm_timeout_seconds,
            verify_ssl=settings.llm_verify_ssl,
        )
    except Exception as exc:
        raise_unless_mock_fallback(settings, "Investigator LLM", exc)
        return None


def build_pr_client(settings: Settings):
    if settings.pr_mode == "github":
        try:
            return RealGitHubPRClient(
                repo_slug=settings.pr_repo_slug,
                token=settings.pr_github_token,
                api_base_url=settings.pr_github_api_base_url,
            ), "github"
        except Exception as exc:
            raise_unless_mock_fallback(settings, "GitHub PR client", exc)
            return MockPRClient(repo_slug=settings.pr_repo_slug), "mock-fallback-startup"

    return MockPRClient(repo_slug=settings.pr_repo_slug), "mock"
