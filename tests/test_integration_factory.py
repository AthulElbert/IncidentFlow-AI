import pytest

from app.config import Settings
from app.services.integration_factory import (
    IntegrationConfigError,
    build_apm_alert_source,
    build_apm_client,
    build_jenkins_client,
    build_jira_client,
    build_llm_client,
    build_pr_client,
)



def _base_settings() -> Settings:
    return Settings(
        storage_backend="json",
        database_url="",
        pr_mode="mock",
        pr_repo_slug="org/agentic-support",
        pr_github_token="",
        pr_github_api_base_url="https://api.github.com",
        pr_base_branch="main",
        pr_local_branch_mode="spec",
        pr_patch_output_dir="generated_patches",
        code_change_mode="spec",
        code_change_allowed_paths="config/,runbooks/",
        code_change_max_lines=200,
        code_change_auto_commit=False,
        code_change_auto_push=False,
        code_fix_mode="template",
        code_fix_target_path="faulty_project/app_config.json",
        code_fix_max_latency_ms=300.0,
        code_fix_max_error_probability=0.02,
        apm_poll_mode="off",
        apm_poll_interval_seconds=30,
        apm_alerts_mode="mock",
        apm_alerts_base_url="http://localhost:9001",
        apm_alerts_dynatrace_token="",
        apm_alerts_timeout_seconds=10,
        apm_alert_queue_file="data/apm_alert_queue.json",
        scheduler_actor="scheduler-agent",
        scheduler_dedup_file="data/processed_alerts.json",
        test_evidence_mode="mock",
        test_evidence_command="python -m pytest -q tests",
        test_evidence_timeout_seconds=120,
        jira_mode="mock",
        jira_base_url="",
        jira_project_key="SUP",
        jira_email="",
        jira_api_token="",
        jenkins_mode="mock",
        jenkins_base_url="https://jenkins.example.local",
        jenkins_user="",
        jenkins_api_token="",
        jenkins_job_suffix="-dev-validation",
        jenkins_prod_job_suffix="-prod-deploy",
        jenkins_verify_ssl=True,
        apm_mode="mock",
        apm_base_url="http://localhost:9001",
        apm_verify_ssl=True,
        apm_timeout_seconds=10,
        triage_mode="heuristic",
        triage_confidence_floor=0.60,
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="",
        llm_model="gpt-4.1-mini",
        llm_timeout_seconds=20,
        llm_verify_ssl=True,
        dev_min_apm_improvement_pct=5.0,
        require_dev_smoke_tests=True,
        min_confidence_for_prod=0.80,
        require_zero_warnings_for_prod=True,
        allowed_jenkins_states_for_prod=["SUCCESS"],
        auth_enabled=True,
        auth_mode="api_key",
        viewer_api_key="viewer-key",
        viewer_actor="viewer-user",
        approver_api_key="approver-key",
        approver_actor="approver-user",
        release_operator_api_key="release-key",
        release_operator_actor="release-user",
        jwt_secret="secret",
        jwt_algorithm="HS256",
        jwt_issuer="issuer",
        jwt_audience="aud",
        jwt_role_claim="role",
        jwt_actor_claim="sub",
        log_level="INFO",
    )


def test_factory_uses_mock_by_default():
    settings = _base_settings()
    _, alert_mode = build_apm_alert_source(settings, "C:/tmp")
    _, jira_mode = build_jira_client(settings)
    _, jenkins_mode = build_jenkins_client(settings)
    _, apm_mode = build_apm_client(settings)
    llm_client, triage_mode = build_llm_client(settings)
    _, pr_mode = build_pr_client(settings)

    assert jira_mode == "mock"
    assert alert_mode == "mock"
    assert jenkins_mode == "mock"
    assert apm_mode == "mock"
    assert llm_client is None
    assert triage_mode == "heuristic"
    assert pr_mode == "mock"


def _misconfigured_real_settings(**overrides) -> Settings:
    return Settings(
        **{
            **_base_settings().__dict__,
            "jira_mode": "real",
            "jenkins_mode": "real",
            "apm_mode": "http",
            "apm_base_url": "",
            "apm_alerts_mode": "http",
            "apm_alerts_base_url": "",
            "triage_mode": "llm",
            "llm_api_key": "",
            "pr_mode": "github",
            "pr_github_token": "",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "builder",
    [
        lambda s: build_jira_client(s),
        lambda s: build_jenkins_client(s),
        lambda s: build_apm_client(s),
        lambda s: build_apm_alert_source(s, "C:/tmp"),
        lambda s: build_llm_client(s),
        lambda s: build_pr_client(s),
    ],
)
def test_real_mode_that_cannot_start_fails_loudly_by_default(builder):
    with pytest.raises(IntegrationConfigError, match="ALLOW_MOCK_FALLBACK"):
        builder(_misconfigured_real_settings())


def test_llm_client_is_built_for_code_fix_even_when_triage_is_heuristic():
    settings = Settings(
        **{
            **_base_settings().__dict__,
            "triage_mode": "heuristic",
            "code_fix_mode": "llm",
            "llm_api_key": "key",
        }
    )
    client, triage_mode = build_llm_client(settings)
    assert client is not None
    assert triage_mode == "heuristic"


def test_factory_startup_fallback_only_when_explicitly_allowed():
    settings = _misconfigured_real_settings(allow_mock_fallback=True)

    _, alert_mode = build_apm_alert_source(settings, "C:/tmp")
    _, jira_mode = build_jira_client(settings)
    _, jenkins_mode = build_jenkins_client(settings)
    _, apm_mode = build_apm_client(settings)
    llm_client, triage_mode = build_llm_client(settings)
    _, pr_mode = build_pr_client(settings)

    assert jira_mode == "mock-fallback-startup"
    assert alert_mode == "mock-fallback-startup"
    assert jenkins_mode == "mock-fallback-startup"
    assert apm_mode == "mock-fallback-startup"
    assert llm_client is None
    assert triage_mode == "heuristic-fallback-startup"
    assert pr_mode == "mock-fallback-startup"


def test_factory_dynatrace_alert_source_mode():
    settings = _base_settings()
    settings = Settings(**{**settings.__dict__, "apm_alerts_mode": "dynatrace", "apm_alerts_dynatrace_token": "x"})
    _, mode = build_apm_alert_source(settings, "C:/tmp")
    assert mode == "dynatrace"
