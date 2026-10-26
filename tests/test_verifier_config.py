import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import ConfigError, load_settings, validate_settings

ROOT = Path(__file__).resolve().parents[1]


def test_verifier_defaults_are_safe_and_valid():
    settings = load_settings()

    assert settings.dev_metrics_mode == "mock" and settings.mock_dev_effect == "resolves"
    assert settings.verifier_min_improvement == 0.5 and settings.verifier_soak_minutes == 10.0
    assert settings.verifier_deploy_wait_seconds == 120.0 and settings.verifier_poll_interval_seconds == 5.0
    validate_settings(settings)


@pytest.mark.parametrize(
    "env,message",
    [
        ({"DEV_METRICS_MODE": "datadog"}, "DEV_METRICS_MODE"),
        ({"DEV_METRICS_MODE": "prometheus"}, "DEV_METRICS_BASE_URL"),
        ({"MOCK_DEV_EFFECT": "explodes"}, "MOCK_DEV_EFFECT"),
        ({"VERIFIER_MIN_IMPROVEMENT": "0"}, "VERIFIER_MIN_IMPROVEMENT"),
        ({"VERIFIER_MIN_IMPROVEMENT": "1.2"}, "VERIFIER_MIN_IMPROVEMENT"),
        ({"VERIFIER_SOAK_MINUTES": "0"}, "must be positive"),
        ({"VERIFIER_DEPLOY_WAIT_SECONDS": "-5"}, "must be positive"),
    ],
)
def test_invalid_verifier_settings_are_rejected_at_startup(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_the_app_builds_the_verifier_from_settings(monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "config/,runbooks/")
    monkeypatch.setenv("CODE_CHANGE_MAX_LINES", "75")
    monkeypatch.setenv("VERIFIER_MIN_IMPROVEMENT", "0.7")
    monkeypatch.setenv("VERIFIER_SOAK_MINUTES", "15")
    monkeypatch.setenv("VERIFIER_DEPLOY_WAIT_SECONDS", "30")
    monkeypatch.setenv("VERIFIER_POLL_INTERVAL_SECONDS", "3")
    main_module = importlib.reload(main_module)

    verifier = main_module.verifier
    assert verifier.allowed_paths == ["config/", "runbooks/"]
    assert verifier.max_changed_lines == 75
    assert verifier.min_improvement == 0.7 and verifier.soak_minutes == 15
    assert verifier.max_polls == 10  # 30s of waiting at one poll every 3s
    assert main_module.tool_modes["dev_metrics"] == "mock"


def test_the_simulated_dev_environment_is_shared_by_the_deploy_target_and_its_metrics(monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    monkeypatch.setenv("MOCK_DEV_EFFECT", "no_effect")
    main_module = importlib.reload(main_module)
    toolkit = main_module.toolkit

    assert toolkit.deploy_target.__class__.__name__ == "_EnvDeployTarget"
    assert toolkit.dev_metrics._env is toolkit.deploy_target._env
    assert toolkit.deploy_target._env.default_effect == "no_effect"
    assert {"dev_metrics", "deploy_target", "metrics", "sandbox"} <= set(
        toolkit.for_agent("verifier", entity_id="X", sandbox=object()).names
    )


def test_the_verification_script_prints_a_scorecard_and_exits_zero(tmp_path):
    out = tmp_path / "verification.json"
    env = {**os.environ, "APP_SKIP_DOTENV": "true"}

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "replay_verifications.py"), "--output", str(out)],
        capture_output=True, text=True, env=env, timeout=300, cwd=str(tmp_path),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "correct verdicts 100%" in proc.stdout and "false passes 0" in proc.stdout and "secret leaks 0" in proc.stdout
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["cases"] == 13 and report["false_passes"] == 0
    assert {r["case"] for r in report["results"]} >= {"good_fix", "no_effect", "secret_left_in_the_tree"}
