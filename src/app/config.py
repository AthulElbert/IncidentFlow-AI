import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.models.schemas import UNSAFE_BUILD_STATES_FOR_PROD
from app.paths import normalize_rel_path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}



def _as_float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default



def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default



def _as_list(value: str | None, default: list[str]) -> list[str]:
    if value is None or value.strip() == "":
        return default
    return [item.strip().upper() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    storage_backend: str
    database_url: str
    pr_mode: str
    pr_repo_slug: str
    pr_github_token: str
    pr_github_api_base_url: str
    pr_base_branch: str
    pr_local_branch_mode: str
    pr_patch_output_dir: str
    code_change_mode: str
    code_change_allowed_paths: str
    code_change_max_lines: int
    code_change_auto_commit: bool
    code_change_auto_push: bool
    code_fix_mode: str
    code_fix_target_path: str
    code_fix_max_latency_ms: float
    code_fix_max_error_probability: float
    apm_poll_mode: str
    apm_poll_interval_seconds: int
    apm_alerts_mode: str
    apm_alerts_base_url: str
    apm_alerts_dynatrace_token: str
    apm_alerts_timeout_seconds: int
    apm_alert_queue_file: str
    scheduler_actor: str
    scheduler_dedup_file: str
    test_evidence_mode: str
    test_evidence_command: str
    test_evidence_timeout_seconds: int
    jira_mode: str
    jira_base_url: str
    jira_project_key: str
    jira_email: str
    jira_api_token: str
    jenkins_mode: str
    jenkins_base_url: str
    jenkins_user: str
    jenkins_api_token: str
    jenkins_job_suffix: str
    jenkins_prod_job_suffix: str
    jenkins_verify_ssl: bool
    apm_mode: str
    apm_base_url: str
    apm_verify_ssl: bool
    apm_timeout_seconds: int
    triage_mode: str
    triage_confidence_floor: float
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    llm_verify_ssl: bool
    dev_min_apm_improvement_pct: float
    require_dev_smoke_tests: bool
    min_confidence_for_prod: float
    require_zero_warnings_for_prod: bool
    allowed_jenkins_states_for_prod: list[str]
    auth_enabled: bool
    auth_mode: str
    viewer_api_key: str
    viewer_actor: str
    approver_api_key: str
    approver_actor: str
    release_operator_api_key: str
    release_operator_actor: str
    jwt_secret: str
    jwt_algorithm: str
    jwt_issuer: str
    jwt_audience: str
    jwt_role_claim: str
    jwt_actor_claim: str
    log_level: str
    data_dir: str = "data"
    allow_mock_fallback: bool = False
    enforce_separation_of_duties: bool = True
    jenkins_build_wait_seconds: float = 20.0
    jenkins_poll_interval_seconds: float = 2.0
    mock_jenkins_outcome: str = "success"
    mock_jenkins_polls_until_done: int = 1
    correlation_window_minutes: float = 15.0
    metrics_mode: str = "mock"
    metrics_base_url: str = "http://localhost:9090"
    metrics_timeout_seconds: int = 10
    logs_mode: str = "mock"
    logs_file_path: str = ""
    deploy_history_mode: str = "mock"
    deploy_target_mode: str = "mock"
    tool_max_calls_per_run: int = 40
    investigator_mode: str = "heuristic"
    investigator_model: str = ""
    investigation_min_confidence: float = 0.60
    investigation_min_margin: float = 0.10
    investigation_max_llm_steps: int = 3
    dev_metrics_mode: str = "mock"
    dev_metrics_base_url: str = ""
    mock_dev_effect: str = "resolves"
    verifier_deploy_wait_seconds: float = 120.0
    verifier_poll_interval_seconds: float = 5.0
    verifier_soak_minutes: float = 10.0
    verifier_min_improvement: float = 0.5


_TOOL_MODES = {
    "METRICS_MODE": ("metrics_mode", {"mock", "prometheus"}),
    "LOGS_MODE": ("logs_mode", {"mock", "file"}),
    "DEPLOY_HISTORY_MODE": ("deploy_history_mode", {"mock", "changes"}),
    "DEPLOY_TARGET_MODE": ("deploy_target_mode", {"mock", "jenkins"}),
    "INVESTIGATOR_MODE": ("investigator_mode", {"heuristic", "llm"}),
    "DEV_METRICS_MODE": ("dev_metrics_mode", {"mock", "prometheus"}),
    "MOCK_DEV_EFFECT": ("mock_dev_effect", {"resolves", "no_effect", "partial", "regresses_other", "deploy_fails"}),
}


class ConfigError(ValueError):
    """Raised at startup when the configuration is unsafe or inconsistent."""


def validate_settings(settings: "Settings") -> None:
    errors: list[str] = []

    unsafe_states = sorted(
        {item.upper() for item in settings.allowed_jenkins_states_for_prod} & UNSAFE_BUILD_STATES_FOR_PROD
    )
    if unsafe_states:
        errors.append(
            f"ALLOWED_JENKINS_STATES_FOR_PROD must only contain terminal success states, got {unsafe_states}"
        )

    for env_name, (attribute, allowed_modes) in _TOOL_MODES.items():
        value = getattr(settings, attribute)
        if value not in allowed_modes:
            errors.append(f"{env_name} must be one of {sorted(allowed_modes)}, got '{value}'")
    if settings.logs_mode == "file" and not settings.logs_file_path:
        errors.append("LOGS_MODE=file requires LOGS_FILE_PATH")
    if settings.investigator_mode == "llm" and not settings.llm_api_key:
        errors.append("INVESTIGATOR_MODE=llm requires LLM_API_KEY")
    if not 0.0 < settings.investigation_min_confidence <= 1.0:
        errors.append("INVESTIGATION_MIN_CONFIDENCE must be in (0, 1]")
    if not 0.0 <= settings.investigation_min_margin < 1.0:
        errors.append("INVESTIGATION_MIN_MARGIN must be in [0, 1)")
    if settings.dev_metrics_mode == "prometheus" and not settings.dev_metrics_base_url:
        errors.append("DEV_METRICS_MODE=prometheus requires DEV_METRICS_BASE_URL")
    if not 0.0 < settings.verifier_min_improvement <= 1.0:
        errors.append("VERIFIER_MIN_IMPROVEMENT must be in (0, 1]")
    if settings.verifier_soak_minutes <= 0 or settings.verifier_deploy_wait_seconds <= 0:
        errors.append("VERIFIER_SOAK_MINUTES and VERIFIER_DEPLOY_WAIT_SECONDS must be positive")

    if settings.code_fix_mode == "llm":
        if not settings.llm_api_key:
            errors.append("CODE_FIX_MODE=llm requires LLM_API_KEY")
        if settings.code_change_mode == "sandbox_git":
            target = normalize_rel_path(settings.code_fix_target_path)
            allowed = [
                part.strip().replace("\\", "/").rstrip("/")
                for part in settings.code_change_allowed_paths.split(",")
                if part.strip()
            ]
            if not any(target == prefix or target.startswith(prefix + "/") for prefix in allowed):
                errors.append(
                    f"CODE_FIX_TARGET_PATH '{target}' is not under CODE_CHANGE_ALLOWED_PATHS {allowed}"
                )

    if errors:
        raise ConfigError("; ".join(errors))


def load_settings(base_dir: str | None = None) -> Settings:
    # Tests set APP_SKIP_DOTENV so a developer's local .env cannot leak into them.
    if not _as_bool(os.getenv("APP_SKIP_DOTENV")):
        if base_dir:
            env_path = Path(base_dir) / ".env"
            load_dotenv(dotenv_path=env_path, override=False)
        else:
            load_dotenv(override=False)

    return Settings(
        storage_backend=os.getenv("STORAGE_BACKEND", "json").strip().lower(),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        pr_mode=os.getenv("PR_MODE", "mock").strip().lower(),
        pr_repo_slug=os.getenv("PR_REPO_SLUG", "org/agentic-support").strip(),
        pr_github_token=os.getenv("PR_GITHUB_TOKEN", "").strip(),
        pr_github_api_base_url=os.getenv("PR_GITHUB_API_BASE_URL", "https://api.github.com").strip(),
        pr_base_branch=os.getenv("PR_BASE_BRANCH", "main").strip(),
        pr_local_branch_mode=os.getenv("PR_LOCAL_BRANCH_MODE", "spec").strip().lower(),
        pr_patch_output_dir=os.getenv("PR_PATCH_OUTPUT_DIR", "generated_patches").strip(),
        code_change_mode=os.getenv("CODE_CHANGE_MODE", "spec").strip().lower(),
        code_change_allowed_paths=os.getenv("CODE_CHANGE_ALLOWED_PATHS", "config/,runbooks/").strip(),
        code_change_max_lines=_as_int(os.getenv("CODE_CHANGE_MAX_LINES"), default=200),
        code_change_auto_commit=_as_bool(os.getenv("CODE_CHANGE_AUTO_COMMIT"), default=False),
        code_change_auto_push=_as_bool(os.getenv("CODE_CHANGE_AUTO_PUSH"), default=False),
        code_fix_mode=os.getenv("CODE_FIX_MODE", "template").strip().lower(),
        code_fix_target_path=os.getenv("CODE_FIX_TARGET_PATH", "faulty_project/app_config.json").strip(),
        code_fix_max_latency_ms=_as_float(os.getenv("CODE_FIX_MAX_LATENCY_MS"), default=300.0),
        code_fix_max_error_probability=_as_float(os.getenv("CODE_FIX_MAX_ERROR_PROBABILITY"), default=0.02),
        apm_poll_mode=os.getenv("APM_POLL_MODE", "off").strip().lower(),
        apm_poll_interval_seconds=_as_int(os.getenv("APM_POLL_INTERVAL_SECONDS"), default=30),
        apm_alerts_mode=os.getenv("APM_ALERTS_MODE", "mock").strip().lower(),
        apm_alerts_base_url=os.getenv("APM_ALERTS_BASE_URL", "http://localhost:9001").strip(),
        apm_alerts_dynatrace_token=os.getenv("APM_ALERTS_DYNATRACE_TOKEN", "").strip(),
        apm_alerts_timeout_seconds=_as_int(os.getenv("APM_ALERTS_TIMEOUT_SECONDS"), default=10),
        apm_alert_queue_file=os.getenv("APM_ALERT_QUEUE_FILE", "data/apm_alert_queue.json").strip(),
        scheduler_actor=os.getenv("SCHEDULER_ACTOR", "scheduler-agent").strip(),
        scheduler_dedup_file=os.getenv("SCHEDULER_DEDUP_FILE", "data/processed_alerts.json").strip(),
        test_evidence_mode=os.getenv("TEST_EVIDENCE_MODE", "mock").strip().lower(),
        test_evidence_command=os.getenv("TEST_EVIDENCE_COMMAND", "python -m pytest -q tests").strip(),
        test_evidence_timeout_seconds=_as_int(os.getenv("TEST_EVIDENCE_TIMEOUT_SECONDS"), default=120),
        jira_mode=os.getenv("JIRA_MODE", "mock").strip().lower(),
        jira_base_url=os.getenv("JIRA_BASE_URL", "").strip(),
        jira_project_key=os.getenv("JIRA_PROJECT_KEY", "SUP").strip(),
        jira_email=os.getenv("JIRA_EMAIL", "").strip(),
        jira_api_token=os.getenv("JIRA_API_TOKEN", "").strip(),
        jenkins_mode=os.getenv("JENKINS_MODE", "mock").strip().lower(),
        jenkins_base_url=os.getenv("JENKINS_BASE_URL", "https://jenkins.example.local").strip(),
        jenkins_user=os.getenv("JENKINS_USER", "").strip(),
        jenkins_api_token=os.getenv("JENKINS_API_TOKEN", "").strip(),
        jenkins_job_suffix=os.getenv("JENKINS_JOB_SUFFIX", "-dev-validation").strip(),
        jenkins_prod_job_suffix=os.getenv("JENKINS_PROD_JOB_SUFFIX", "-prod-deploy").strip(),
        jenkins_verify_ssl=_as_bool(os.getenv("JENKINS_VERIFY_SSL"), default=True),
        apm_mode=os.getenv("APM_MODE", "mock").strip().lower(),
        apm_base_url=os.getenv("APM_BASE_URL", "http://localhost:9001").strip(),
        apm_verify_ssl=_as_bool(os.getenv("APM_VERIFY_SSL"), default=True),
        apm_timeout_seconds=_as_int(os.getenv("APM_TIMEOUT_SECONDS"), default=10),
        triage_mode=os.getenv("TRIAGE_MODE", "heuristic").strip().lower(),
        triage_confidence_floor=_as_float(os.getenv("TRIAGE_CONFIDENCE_FLOOR"), default=0.60),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "gpt-4.1-mini").strip(),
        llm_timeout_seconds=_as_int(os.getenv("LLM_TIMEOUT_SECONDS"), default=20),
        llm_verify_ssl=_as_bool(os.getenv("LLM_VERIFY_SSL"), default=True),
        dev_min_apm_improvement_pct=_as_float(os.getenv("DEV_MIN_APM_IMPROVEMENT_PCT"), default=5.0),
        require_dev_smoke_tests=_as_bool(os.getenv("REQUIRE_DEV_SMOKE_TESTS"), default=True),
        min_confidence_for_prod=_as_float(os.getenv("MIN_CONFIDENCE_FOR_PROD"), default=0.80),
        require_zero_warnings_for_prod=_as_bool(os.getenv("REQUIRE_ZERO_WARNINGS_FOR_PROD"), default=True),
        allowed_jenkins_states_for_prod=_as_list(
            os.getenv("ALLOWED_JENKINS_STATES_FOR_PROD"),
            default=["SUCCESS"],
        ),
        auth_enabled=_as_bool(os.getenv("AUTH_ENABLED"), default=True),
        auth_mode=os.getenv("AUTH_MODE", "api_key").strip().lower(),
        viewer_api_key=os.getenv("VIEWER_API_KEY", "viewer-local-key").strip(),
        viewer_actor=os.getenv("VIEWER_ACTOR", "viewer-user").strip(),
        approver_api_key=os.getenv("APPROVER_API_KEY", "approver-local-key").strip(),
        approver_actor=os.getenv("APPROVER_ACTOR", "approver-user").strip(),
        release_operator_api_key=os.getenv("RELEASE_OPERATOR_API_KEY", "release-local-key").strip(),
        release_operator_actor=os.getenv("RELEASE_OPERATOR_ACTOR", "release-operator-user").strip(),
        jwt_secret=os.getenv("JWT_SECRET", "dev-jwt-secret").strip(),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256").strip(),
        jwt_issuer=os.getenv("JWT_ISSUER", "agentic-support").strip(),
        jwt_audience=os.getenv("JWT_AUDIENCE", "agentic-support-api").strip(),
        jwt_role_claim=os.getenv("JWT_ROLE_CLAIM", "role").strip(),
        jwt_actor_claim=os.getenv("JWT_ACTOR_CLAIM", "sub").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        data_dir=os.getenv("APP_DATA_DIR", "data").strip() or "data",
        allow_mock_fallback=_as_bool(os.getenv("ALLOW_MOCK_FALLBACK"), default=False),
        enforce_separation_of_duties=_as_bool(os.getenv("ENFORCE_SEPARATION_OF_DUTIES"), default=True),
        jenkins_build_wait_seconds=_as_float(os.getenv("JENKINS_BUILD_WAIT_SECONDS"), default=20.0),
        jenkins_poll_interval_seconds=_as_float(os.getenv("JENKINS_POLL_INTERVAL_SECONDS"), default=2.0),
        mock_jenkins_outcome=os.getenv("MOCK_JENKINS_OUTCOME", "success").strip().lower(),
        mock_jenkins_polls_until_done=_as_int(os.getenv("MOCK_JENKINS_POLLS"), default=1),
        correlation_window_minutes=_as_float(os.getenv("CORRELATION_WINDOW_MINUTES"), default=15.0),
        metrics_mode=os.getenv("METRICS_MODE", "mock").strip().lower(),
        metrics_base_url=os.getenv("METRICS_BASE_URL", "http://localhost:9090").strip(),
        metrics_timeout_seconds=_as_int(os.getenv("METRICS_TIMEOUT_SECONDS"), default=10),
        logs_mode=os.getenv("LOGS_MODE", "mock").strip().lower(),
        logs_file_path=os.getenv("LOGS_FILE_PATH", "").strip(),
        deploy_history_mode=os.getenv("DEPLOY_HISTORY_MODE", "mock").strip().lower(),
        deploy_target_mode=os.getenv("DEPLOY_TARGET_MODE", "mock").strip().lower(),
        tool_max_calls_per_run=_as_int(os.getenv("TOOL_MAX_CALLS_PER_RUN"), default=40),
        investigator_mode=os.getenv("INVESTIGATOR_MODE", "heuristic").strip().lower(),
        investigator_model=os.getenv("INVESTIGATOR_MODEL", "").strip(),
        investigation_min_confidence=_as_float(os.getenv("INVESTIGATION_MIN_CONFIDENCE"), default=0.60),
        investigation_min_margin=_as_float(os.getenv("INVESTIGATION_MIN_MARGIN"), default=0.10),
        investigation_max_llm_steps=_as_int(os.getenv("INVESTIGATION_MAX_LLM_STEPS"), default=3),
        dev_metrics_mode=os.getenv("DEV_METRICS_MODE", "mock").strip().lower(),
        dev_metrics_base_url=os.getenv("DEV_METRICS_BASE_URL", "").strip(),
        mock_dev_effect=os.getenv("MOCK_DEV_EFFECT", "resolves").strip().lower(),
        verifier_deploy_wait_seconds=_as_float(os.getenv("VERIFIER_DEPLOY_WAIT_SECONDS"), default=120.0),
        verifier_poll_interval_seconds=_as_float(os.getenv("VERIFIER_POLL_INTERVAL_SECONDS"), default=5.0),
        verifier_soak_minutes=_as_float(os.getenv("VERIFIER_SOAK_MINUTES"), default=10.0),
        verifier_min_improvement=_as_float(os.getenv("VERIFIER_MIN_IMPROVEMENT"), default=0.5),
    )
