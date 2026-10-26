import pytest


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch, tmp_path):
    """Run every test against throwaway state and simulators only.

    Without this, tests that import app.main read the developer's .env (which may
    enable a real LLM, sandbox_git, etc.) and write into the tracked data/ files.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setenv("APP_SKIP_DOTENV", "true")
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SCHEDULER_DEDUP_FILE", str(data_dir / "processed_alerts.json"))
    monkeypatch.setenv("APM_ALERT_QUEUE_FILE", str(data_dir / "apm_alert_queue.json"))
    monkeypatch.setenv("PR_PATCH_OUTPUT_DIR", str(tmp_path / "generated_patches"))

    for key, value in {
        "STORAGE_BACKEND": "json",
        "JIRA_MODE": "mock",
        "JENKINS_MODE": "mock",
        "APM_MODE": "mock",
        "APM_ALERTS_MODE": "mock",
        "APM_POLL_MODE": "off",
        "PR_MODE": "mock",
        "PR_LOCAL_BRANCH_MODE": "spec",
        "CODE_CHANGE_MODE": "spec",
        "CODE_FIX_MODE": "template",
        "TRIAGE_MODE": "heuristic",
        "TEST_EVIDENCE_MODE": "mock",
        "AUTH_ENABLED": "true",
        "AUTH_MODE": "api_key",
        # Mock Jenkins finishes on the second poll and tests never sleep between polls.
        "MOCK_JENKINS_POLLS": "1",
        "MOCK_JENKINS_OUTCOME": "success",
        "JENKINS_BUILD_WAIT_SECONDS": "20",
        "JENKINS_POLL_INTERVAL_SECONDS": "0",
    }.items():
        monkeypatch.setenv(key, value)

    for key in (
        "DATABASE_URL",
        "LLM_API_KEY",
        "ALLOWED_JENKINS_STATES_FOR_PROD",
        "ALLOW_MOCK_FALLBACK",
        "ENFORCE_SEPARATION_OF_DUTIES",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def make_app(monkeypatch):
    """Build the real app with agent keys set, and return (module, client). Worktrees are refused unless a test supplies a provider."""

    def _make(**env):
        import importlib

        from agent_harness import ExplodingProvider
        from fastapi.testclient import TestClient

        monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
        monkeypatch.setenv("APPROVER_API_KEY", "approver-key")
        monkeypatch.setenv("RELEASE_OPERATOR_API_KEY", "release-key")
        monkeypatch.setenv("VERIFIER_POLL_INTERVAL_SECONDS", "0.01")
        monkeypatch.setenv("MOCK_JENKINS_POLLS", "0")
        monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "faulty_project/,config/,src/")
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        import app.main as main_module

        main_module = importlib.reload(main_module)
        main_module.agent_runtime.sandbox_provider = ExplodingProvider()
        return main_module, TestClient(main_module.app)

    return _make
