import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv



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



def load_settings(base_dir: str | None = None) -> Settings:
    if base_dir:
        env_path = Path(base_dir) / ".env"
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    return Settings(
        storage_backend=os.getenv("STORAGE_BACKEND", "json").strip().lower(),
        database_url=os.getenv("DATABASE_URL", "").strip(),
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
            default=["QUEUED", "SUCCESS"],
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
    )
