import pytest

from app.config import ConfigError, load_settings, validate_settings


def test_defaults_are_safe_and_valid():
    settings = load_settings()

    assert settings.allowed_jenkins_states_for_prod == ["SUCCESS"]
    assert settings.allow_mock_fallback is False
    validate_settings(settings)


def test_non_terminal_jenkins_state_for_prod_is_rejected(monkeypatch):
    monkeypatch.setenv("ALLOWED_JENKINS_STATES_FOR_PROD", "QUEUED,SUCCESS")

    with pytest.raises(ConfigError, match="ALLOWED_JENKINS_STATES_FOR_PROD"):
        validate_settings(load_settings())


def test_llm_code_fix_requires_an_api_key(monkeypatch):
    monkeypatch.setenv("CODE_FIX_MODE", "llm")

    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        validate_settings(load_settings())


def test_llm_code_fix_target_must_be_inside_allowed_paths(monkeypatch):
    monkeypatch.setenv("CODE_FIX_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("CODE_CHANGE_MODE", "sandbox_git")
    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "config/,runbooks/")
    monkeypatch.setenv("CODE_FIX_TARGET_PATH", "faulty_project/app_config.json")

    with pytest.raises(ConfigError, match="CODE_CHANGE_ALLOWED_PATHS"):
        validate_settings(load_settings())

    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "config/,faulty_project/")
    validate_settings(load_settings())


def test_dotenv_is_skipped_when_requested(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("JIRA_MODE=real\n", encoding="utf-8")
    monkeypatch.delenv("JIRA_MODE", raising=False)

    assert load_settings(str(tmp_path)).jira_mode == "mock"  # APP_SKIP_DOTENV is set by conftest

    monkeypatch.setenv("APP_SKIP_DOTENV", "false")
    assert load_settings(str(tmp_path)).jira_mode == "real"
