import pytest

from app.adapters.pr_client import MockPRClient
from app.config import ConfigError, load_settings, validate_settings
from app.paths import normalize_rel_path
from app.services.pr_preparer import PRPreparationService


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("./config/app.yaml", "config/app.yaml"),
        ("config\\app.yaml", "config/app.yaml"),
        (".env", ".env"),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
        ("././x", "x"),
        (".", ""),
        ("  ", ""),
    ],
)
def test_normalize_keeps_leading_dots_that_belong_to_the_name(raw, expected):
    assert normalize_rel_path(raw) == expected


def test_pr_preparer_does_not_mistake_dot_github_for_an_allowed_github_dir(tmp_path):
    service = PRPreparationService(
        pr_client=MockPRClient(), repo_root=str(tmp_path), code_change_allowed_paths="config/,github/"
    )

    assert service._validate_allowed_path("config/app.yaml")[0] is True
    assert service._validate_allowed_path("./config/app.yaml")[0] is True
    assert service._validate_allowed_path("github/notes.md")[0] is True
    assert service._validate_allowed_path(".github/workflows/ci.yml")[0] is False
    assert service._validate_allowed_path(".env")[0] is False


def test_startup_validation_does_not_mistake_a_dot_directory_for_an_allowed_one(monkeypatch):
    monkeypatch.setenv("CODE_FIX_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("CODE_CHANGE_MODE", "sandbox_git")
    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "config/,github/")
    monkeypatch.setenv("CODE_FIX_TARGET_PATH", ".github/workflows/ci.yml")

    with pytest.raises(ConfigError, match="CODE_CHANGE_ALLOWED_PATHS"):
        validate_settings(load_settings())
