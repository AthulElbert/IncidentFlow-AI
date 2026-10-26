import importlib

import pytest

from app.config import ConfigError, load_settings, validate_settings
from app.services.integration_factory import (
    build_agent_llm,
    build_critic_llm,
    build_fixer_llm,
    build_investigator_llm,
    build_llm_client,
)


def test_fixer_is_off_and_critic_is_rules_only_by_default():
    settings = load_settings()

    assert (settings.fixer_mode, settings.fixer_max_steps) == ("off", 12)
    assert settings.critic_mode == "rules"
    validate_settings(settings)
    assert build_fixer_llm(settings, object()) is None and build_critic_llm(settings, object()) is None


@pytest.mark.parametrize(
    "env,message",
    [
        ({"FIXER_MODE": "wizard"}, "FIXER_MODE"),
        ({"FIXER_MODE": "llm"}, "FIXER_MODE=llm requires LLM_API_KEY"),
        ({"CRITIC_MODE": "psychic"}, "CRITIC_MODE"),
        ({"CRITIC_MODE": "llm"}, "CRITIC_MODE=llm requires LLM_API_KEY"),
        ({"FIXER_MAX_STEPS": "0"}, "FIXER_MAX_STEPS"),
        ({"FIXER_MAX_STEPS": "500"}, "FIXER_MAX_STEPS"),
    ],
)
def test_invalid_agent_settings_are_rejected_at_startup(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_the_shared_client_is_built_when_only_the_fixer_or_the_critic_needs_it(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "key")
    for variable in ("FIXER_MODE", "CRITIC_MODE"):
        monkeypatch.setenv(variable, "llm")
        client, triage_mode = build_llm_client(load_settings())
        assert client is not None and triage_mode == "heuristic"
        monkeypatch.delenv(variable)


def test_each_agent_can_have_its_own_model_or_share_the_main_one(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "main")
    monkeypatch.setenv("FIXER_MODE", "llm")
    monkeypatch.setenv("CRITIC_MODE", "llm")
    monkeypatch.setenv("INVESTIGATOR_MODE", "llm")
    monkeypatch.setenv("CRITIC_MODEL", "sceptical-model")
    settings = load_settings()
    shared, _ = build_llm_client(settings)

    assert build_fixer_llm(settings, shared) is shared
    assert build_investigator_llm(settings, shared) is shared
    critic = build_critic_llm(settings, shared)
    assert critic is not shared and critic.model == "sceptical-model" and shared.model == "main"
    assert build_agent_llm(settings, "X", False, "whatever", shared) is None


def test_the_app_builds_both_agents_from_settings(monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "config/,src/")
    monkeypatch.setenv("CODE_CHANGE_MAX_LINES", "60")
    monkeypatch.setenv("VERIFIER_MIN_IMPROVEMENT", "0.6")
    main_module = importlib.reload(main_module)
    assert main_module.fixer.llm is None and main_module.critic.llm is None
    assert main_module.fixer.editable_paths == ["config/", "src/"] and main_module.fixer.max_changed_lines == 60
    assert main_module.critic.min_improvement == 0.6

    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("FIXER_MODE", "llm")
    monkeypatch.setenv("FIXER_MODEL", "coder-model")
    monkeypatch.setenv("FIXER_MAX_STEPS", "9")
    monkeypatch.setenv("CRITIC_MODE", "llm")
    main_module = importlib.reload(main_module)
    assert main_module.fixer.llm.model == "coder-model" and main_module.fixer.max_steps == 9
    assert main_module.critic.llm is not None
