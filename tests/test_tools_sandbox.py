import os
import subprocess
import sys

import pytest

from app.tools.base import ToolError
from app.tools.sandbox import FileSandbox, GitWorktreeProvider


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "config" / "app.yaml").write_text("timeout_ms: 800\nretries: 1\n", encoding="utf-8")
    (root / "src" / "main.py").write_text("print('hello')\n# TODO fix timeout\n", encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (root / ".env").write_text("LLM_API_KEY=super-secret\n", encoding="utf-8")
    (root / "secrets" / "token.txt").write_text("hunter2\n", encoding="utf-8")
    (root / "server.pem").write_text("-----BEGIN-----\n", encoding="utf-8")
    (root / "Dockerfile").write_text("FROM python\n", encoding="utf-8")
    return root


@pytest.fixture
def sandbox(repo):
    return FileSandbox(repo, allowed_edit_paths=["config/"], max_edit_lines=20)


# --- confinement ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "config/../../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "C:\\Windows\\win.ini", "..\\x"],
)
def test_paths_outside_the_repo_are_refused(sandbox, path):
    with pytest.raises(ToolError, match="relative|escapes"):
        sandbox.read(path)
    with pytest.raises(ToolError, match="relative|escapes"):
        sandbox.edit(path, "x")


def test_a_symlink_pointing_outside_the_repo_is_refused(sandbox, repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        os.symlink(outside, repo / "config" / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")

    with pytest.raises(ToolError, match="escapes"):
        sandbox.read("config/link.txt")
    with pytest.raises(ToolError, match="escapes"):
        sandbox.edit("config/link.txt", "overwritten")
    assert outside.read_text(encoding="utf-8") == "private"
    assert "config/link.txt" not in sandbox.list_files()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_a_directory_junction_pointing_outside_the_repo_is_refused(sandbox, repo, tmp_path):
    # Junctions need no privileges (unlike symlinks), so this exercises the same escape on Windows.
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(repo / "config" / "jump"), str(outside)], capture_output=True, text=True
    )
    if made.returncode != 0:
        pytest.skip(f"could not create junction: {made.stdout} {made.stderr}")

    with pytest.raises(ToolError, match="escapes"):
        sandbox.read("config/jump/secret.txt")
    with pytest.raises(ToolError, match="escapes"):
        sandbox.edit("config/jump/secret.txt", "overwritten")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "private"
    # Listing and grepping must not walk through the junction either.
    assert not any("jump" in item or "secret.txt" in item for item in sandbox.list_files())
    assert sandbox.grep("private") == []


# --- reading ----------------------------------------------------------------------------


def test_secrets_cannot_be_read_listed_or_grepped(sandbox):
    for path in [".env", "secrets/token.txt", "server.pem", ".git/config"]:
        with pytest.raises(ToolError, match="not allowed|no such file"):
            sandbox.read(path)

    listed = sandbox.list_files()
    assert "config/app.yaml" in listed and "src/main.py" in listed
    assert not any(item.startswith(("secrets", ".git/")) or item in {".env", "server.pem"} for item in listed)
    assert sandbox.grep("super-secret|hunter2|BEGIN") == []


def test_read_list_and_grep_work_on_ordinary_files(sandbox):
    assert "timeout_ms: 800" in sandbox.read("config/app.yaml")
    assert sandbox.list_files("config") == ["config/app.yaml"]

    matches = sandbox.grep(r"timeout", "")
    assert {(m.path, m.line) for m in matches} == {("config/app.yaml", 1), ("src/main.py", 2)}
    assert sandbox.grep("timeout", "config")[0].text == "timeout_ms: 800"
    with pytest.raises(ToolError, match="invalid pattern"):
        sandbox.grep("(")
    with pytest.raises(ToolError, match="no such file"):
        sandbox.read("config/missing.yaml")
    with pytest.raises(ToolError, match="no such directory"):
        sandbox.list_files("nope")


def test_oversized_files_are_not_dumped_into_a_prompt(sandbox, repo):
    (repo / "config" / "big.log").write_text("x" * (300 * 1024), encoding="utf-8")

    with pytest.raises(ToolError, match="larger than"):
        sandbox.read("config/big.log")


# --- editing ----------------------------------------------------------------------------


def test_edit_inside_the_allowed_paths_and_diff(sandbox, repo):
    result = sandbox.edit("config/app.yaml", "timeout_ms: 300\nretries: 1\n")

    assert (result.path, result.lines, result.changed) == ("config/app.yaml", 2, True)
    assert (repo / "config" / "app.yaml").read_text(encoding="utf-8") == "timeout_ms: 300\nretries: 1\n"
    diff = sandbox.diff()
    assert "--- a/config/app.yaml" in diff and "+++ b/config/app.yaml" in diff
    assert "-timeout_ms: 800" in diff and "+timeout_ms: 300" in diff


def test_editing_to_the_same_content_is_a_noop_and_new_files_diff_from_nothing(sandbox):
    assert sandbox.edit("config/app.yaml", "timeout_ms: 800\nretries: 1\n").changed is False
    assert sandbox.diff() == ""

    sandbox.edit("config/new.yaml", "a: 1\n")
    assert "--- /dev/null" in sandbox.diff() and "+a: 1" in sandbox.diff()


@pytest.mark.parametrize(
    "path,message",
    [
        ("src/main.py", "outside the editable paths"),
        (".github/workflows/ci.yml", "not allowed"),
        ("Dockerfile", "not allowed"),
        (".env", "not allowed"),
        ("config/.env", "not allowed"),
        ("config/id_rsa", "not allowed"),
    ],
)
def test_edits_outside_the_allow_list_or_to_protected_files_are_refused(sandbox, repo, path, message):
    before = {p: p.read_text(encoding="utf-8") for p in repo.rglob("*") if p.is_file()}

    with pytest.raises(ToolError, match=message):
        sandbox.edit(path, "tampered\n")

    assert {p: p.read_text(encoding="utf-8") for p in repo.rglob("*") if p.is_file()} == before
    assert sandbox.diff() == ""


def test_dot_directories_are_not_confused_with_their_stripped_names(repo):
    # A naive lstrip("./") turns ".github" into "github" and ".env" into "env".
    permissive = FileSandbox(repo, allowed_edit_paths=["github", "env", "config"])

    with pytest.raises(ToolError, match="not allowed"):
        permissive.edit(".github/workflows/ci.yml", "x")
    with pytest.raises(ToolError, match="not allowed"):
        permissive.edit(".env", "x")


def test_oversized_edits_are_refused(sandbox):
    with pytest.raises(ToolError, match="exceeds the limit of 20"):
        sandbox.edit("config/app.yaml", "line\n" * 21)


def test_no_editable_paths_means_read_only(repo):
    with pytest.raises(ToolError, match="no editable paths"):
        FileSandbox(repo, allowed_edit_paths=[]).edit("config/app.yaml", "x")


def test_sandbox_root_must_exist(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        FileSandbox(tmp_path / "missing", allowed_edit_paths=[])


# --- running tests ----------------------------------------------------------------------


def _python_command(script: str) -> str:
    if " " in sys.executable:
        pytest.skip("interpreter path contains spaces")
    return f"{sys.executable} {script}"


def test_run_tests_reports_pass_and_fail(repo):
    (repo / "ok.py").write_text("print('all good')\n", encoding="utf-8")
    (repo / "bad.py").write_text("import sys\nprint('boom')\nsys.exit(4)\n", encoding="utf-8")

    passed = FileSandbox(repo, ["config"], test_command=_python_command("ok.py")).run_tests()
    failed = FileSandbox(repo, ["config"], test_command=_python_command("bad.py")).run_tests()

    assert passed.passed and passed.exit_code == 0 and "all good" in passed.output
    assert not failed.passed and failed.exit_code == 4 and "boom" in failed.output


def test_run_tests_cannot_see_the_platforms_secrets(repo, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-live-123")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db/x")
    monkeypatch.setenv("PR_GITHUB_TOKEN", "ghp_abc")
    (repo / "leak.py").write_text(
        "import os\nleaked = [k for k in ('LLM_API_KEY','DATABASE_URL','PR_GITHUB_TOKEN') if k in os.environ]\n"
        "print('LEAKED', leaked)\n",
        encoding="utf-8",
    )

    result = FileSandbox(repo, ["config"], test_command=_python_command("leak.py")).run_tests()

    assert result.passed
    assert "LEAKED []" in result.output


def test_run_tests_times_out(repo):
    (repo / "slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    result = FileSandbox(repo, ["config"], test_command=_python_command("slow.py"), test_timeout_seconds=1).run_tests()

    assert not result.passed
    assert "timed out" in result.output


def test_run_tests_with_a_missing_command_is_a_tool_error(repo):
    with pytest.raises(ToolError, match="could not run tests"):
        FileSandbox(repo, ["config"], test_command="definitely-not-a-real-program --x").run_tests()


# --- git worktree provider --------------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def test_worktree_provider_opens_an_isolated_checkout_on_a_new_branch(tmp_path):
    repo = tmp_path / "project"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "app.yaml").write_text("timeout_ms: 800\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")

    provider = GitWorktreeProvider(repo, allowed_edit_paths=["config"], max_edit_lines=20)
    sandbox = provider.open("agent/chg-1-payments")
    sandbox.edit("config/app.yaml", "timeout_ms: 300\n")

    assert sandbox.root.parent.name == ".agent_worktrees"
    assert (sandbox.root / "config" / "app.yaml").read_text(encoding="utf-8") == "timeout_ms: 300\n"
    # The main checkout is untouched; only the worktree changed.
    assert (repo / "config" / "app.yaml").read_text(encoding="utf-8") == "timeout_ms: 800\n"
    branches = subprocess.run(["git", "branch", "--list", "agent/chg-1-payments"], cwd=repo, capture_output=True, text=True)
    assert "agent/chg-1-payments" in branches.stdout


def test_worktree_provider_reports_git_failures(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(ToolError, match="could not create worktree"):
        GitWorktreeProvider(not_a_repo, allowed_edit_paths=["config"]).open("agent/x")
    with pytest.raises(ToolError, match="branch name is required"):
        GitWorktreeProvider(not_a_repo, allowed_edit_paths=["config"]).open("  ")
