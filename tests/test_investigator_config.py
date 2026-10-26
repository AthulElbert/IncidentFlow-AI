import importlib

import pytest

from app.config import ConfigError, load_settings, validate_settings
from app.services.integration_factory import build_investigator_llm, build_llm_client


def test_defaults_are_the_evidence_based_investigator_with_sensible_thresholds():
    settings = load_settings()

    assert settings.investigator_mode == "heuristic"
    assert (settings.investigation_min_confidence, settings.investigation_min_margin) == (0.60, 0.10)
    assert settings.investigation_max_llm_steps == 3
    validate_settings(settings)


@pytest.mark.parametrize(
    "env,message",
    [
        ({"INVESTIGATOR_MODE": "oracle"}, "INVESTIGATOR_MODE"),
        ({"INVESTIGATOR_MODE": "llm"}, "requires LLM_API_KEY"),
        ({"INVESTIGATION_MIN_CONFIDENCE": "0"}, "INVESTIGATION_MIN_CONFIDENCE"),
        ({"INVESTIGATION_MIN_CONFIDENCE": "1.5"}, "INVESTIGATION_MIN_CONFIDENCE"),
        ({"INVESTIGATION_MIN_MARGIN": "-0.1"}, "INVESTIGATION_MIN_MARGIN"),
    ],
)
def test_invalid_investigator_settings_are_rejected_at_startup(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_no_investigator_model_is_built_in_heuristic_mode():
    assert build_investigator_llm(load_settings(), shared_client=object()) is None


def test_the_investigator_shares_the_main_client_unless_given_its_own_model(monkeypatch):
    monkeypatch.setenv("INVESTIGATOR_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    settings = load_settings()
    shared, _ = build_llm_client(settings)
    assert shared is not None and shared.model == "main-model"

    assert build_investigator_llm(settings, shared) is shared

    monkeypatch.setenv("INVESTIGATOR_MODEL", "stronger-model")
    own = build_investigator_llm(load_settings(), shared)
    assert own is not shared and own.model == "stronger-model" and shared.model == "main-model"


def test_the_main_client_is_built_when_only_the_investigator_needs_it(monkeypatch):
    monkeypatch.setenv("INVESTIGATOR_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "key")

    client, triage_mode = build_llm_client(load_settings())

    assert client is not None
    assert triage_mode == "heuristic"  # triage itself still does not use the model


def test_the_app_builds_the_investigator_from_settings(monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    monkeypatch.setenv("INVESTIGATION_MIN_CONFIDENCE", "0.7")
    main_module = importlib.reload(main_module)
    assert main_module.investigator.llm is None
    assert main_module.investigator.min_confidence == 0.7

    monkeypatch.setenv("INVESTIGATOR_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("INVESTIGATOR_MODEL", "stronger-model")
    main_module = importlib.reload(main_module)
    assert main_module.investigator.llm is not None
    assert main_module.investigator.llm.model == "stronger-model"
