from pathlib import Path

from app.adapters.apm_client import HttpAPMClient, MockAPMClient
from app.adapters.apm_alert_source import DynatraceAPMAlertSource, HttpAPMAlertSource, MockAPMAlertSource
from app.adapters.jenkins_client import MockJenkinsClient, RealJenkinsClient
from app.adapters.jira_client import MockJiraClient, RealJiraClient
from app.adapters.llm_client import OpenAICompatibleLLMClient
from app.adapters.pr_client import MockPRClient, RealGitHubPRClient
from app.config import Settings


def build_jira_client(settings: Settings):
    if settings.jira_mode == "real":
        try:
            return RealJiraClient(
                base_url=settings.jira_base_url,
                project_key=settings.jira_project_key,
                email=settings.jira_email,
                api_token=settings.jira_api_token,
            ), "real"
        except Exception:
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
        except Exception:
            return MockJenkinsClient(
                base_url=settings.jenkins_base_url,
                job_suffix=settings.jenkins_job_suffix,
                prod_job_suffix=settings.jenkins_prod_job_suffix,
            ), "mock-fallback-startup"

    return MockJenkinsClient(
        base_url=settings.jenkins_base_url,
        job_suffix=settings.jenkins_job_suffix,
        prod_job_suffix=settings.jenkins_prod_job_suffix,
    ), "mock"


def build_apm_client(settings: Settings):
    if settings.apm_mode == "http":
        try:
            return HttpAPMClient(
                base_url=settings.apm_base_url,
                timeout_seconds=settings.apm_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            ), "http"
        except Exception:
            return MockAPMClient(), "mock-fallback-startup"

    return MockAPMClient(), "mock"


def build_apm_alert_source(settings: Settings, base_dir: str):
    if settings.apm_alerts_mode == "dynatrace":
        try:
            return DynatraceAPMAlertSource(
                base_url=settings.apm_alerts_base_url,
                api_token=settings.apm_alerts_dynatrace_token,
                timeout_seconds=settings.apm_alerts_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            ), "dynatrace"
        except Exception:
            queue_path = str(Path(base_dir) / settings.apm_alert_queue_file)
            return MockAPMAlertSource(queue_file=queue_path), "mock-fallback-startup"

    if settings.apm_alerts_mode == "http":
        try:
            return HttpAPMAlertSource(
                base_url=settings.apm_alerts_base_url,
                timeout_seconds=settings.apm_alerts_timeout_seconds,
                verify_ssl=settings.apm_verify_ssl,
            ), "http"
        except Exception:
            queue_path = str(Path(base_dir) / settings.apm_alert_queue_file)
            return MockAPMAlertSource(queue_file=queue_path), "mock-fallback-startup"

    queue_path = str(Path(base_dir) / settings.apm_alert_queue_file)
    return MockAPMAlertSource(queue_file=queue_path), "mock"


def build_llm_client(settings: Settings):
    if settings.triage_mode == "llm":
        try:
            return OpenAICompatibleLLMClient(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
                verify_ssl=settings.llm_verify_ssl,
            ), "llm"
        except Exception:
            return None, "heuristic-fallback-startup"

    return None, "heuristic"


def build_pr_client(settings: Settings):
    if settings.pr_mode == "github":
        try:
            return RealGitHubPRClient(
                repo_slug=settings.pr_repo_slug,
                token=settings.pr_github_token,
                api_base_url=settings.pr_github_api_base_url,
            ), "github"
        except Exception:
            return MockPRClient(repo_slug=settings.pr_repo_slug), "mock-fallback-startup"

    return MockPRClient(repo_slug=settings.pr_repo_slug), "mock"
