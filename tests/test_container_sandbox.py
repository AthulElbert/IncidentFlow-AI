"""Running a proposed fix's tests in a locked-down container instead of on the host.

The first part uses a fake Docker and runs everywhere. The second runs real containers and is skipped unless
Docker is reachable and the sandbox image is built (docker build -f ops/sandbox.Dockerfile -t incidentflow-sandbox:latest .).
"""
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from agent_harness import CONFIG_PATH, FIXED, build, done, edit, make_repo

from app.config import ConfigError, load_settings, validate_settings
from app.tools.base import ToolError
from app.tools.sandbox import FileSandbox
from app.tools.test_runners import ContainerTestRunner

IMAGE = "incidentflow-sandbox:latest"


class FakeDocker:
    """Records the commands it is given and answers with a scripted result."""

    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if self.raises is not None and args[1] == "run":
            raise self.raises
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def _runner(fake, **kwargs):
    return ContainerTestRunner(IMAGE, runner=fake, **kwargs)


# --- the command that is run ---------------------------------------------------------------------


def test_the_container_is_started_locked_down(tmp_path):
    args = ContainerTestRunner(IMAGE, memory="768m", cpus="2", pids_limit=99, tmpfs_mb=128).docker_args(tmp_path, "c1")
    joined = " ".join(args)

    for flag in (
        "--rm", "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
        "--user 65534:65534", "--pids-limit 99", "--memory 768m", "--memory-swap 768m", "--cpus 2",
        "--tmpfs /tmp:rw,size=128m", "--name c1", "--workdir /work",
    ):
        assert flag in joined, flag
    assert f"{tmp_path.resolve()}:/work:ro" in args  # the worktree can be read, not changed
    assert args[-6:] == [IMAGE, "python", "-m", "pytest", "-q", "tests"]  # the image, then the test command


def test_no_host_environment_is_passed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-very-secret")
    args = ContainerTestRunner(IMAGE).docker_args(tmp_path, "c1")

    passed = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
    assert passed == ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1", "PYTEST_ADDOPTS=-p no:cacheprovider"]
    assert "sk-very-secret" not in " ".join(args)


# --- what the result means ---------------------------------------------------------------------------


def test_passing_and_failing_tests_are_reported_as_such(tmp_path):
    passed = _runner(FakeDocker(0, "3 passed")).run(tmp_path)
    failed = _runner(FakeDocker(1, "1 failed", "AssertionError")).run(tmp_path)

    assert passed.passed and passed.exit_code == 0 and "3 passed" in passed.output
    assert not failed.passed and failed.exit_code == 1 and "AssertionError" in failed.output


@pytest.mark.parametrize("code", [125, 126, 127])
def test_a_docker_failure_is_not_reported_as_failing_tests(tmp_path, code):
    with pytest.raises(ToolError, match=f"could not run \\(docker exit {code}\\)"):
        _runner(FakeDocker(code, "", "Unable to find image")).run(tmp_path)


def test_docker_not_installed_is_a_tool_error(tmp_path):
    with pytest.raises(ToolError, match="could not start the test container"):
        _runner(FakeDocker(raises=FileNotFoundError("docker"))).run(tmp_path)


def test_a_test_run_that_takes_too_long_is_stopped_and_its_container_removed(tmp_path):
    fake = FakeDocker(raises=subprocess.TimeoutExpired("docker", 5))

    result = _runner(fake, timeout_seconds=5).run(tmp_path)

    assert not result.passed and result.exit_code == -1 and "timed out after 5s" in result.output
    name = fake.calls[0][fake.calls[0].index("--name") + 1]
    assert fake.calls[1] == ["docker", "rm", "--force", name]


def test_startup_check_fails_closed_when_the_image_or_docker_is_missing():
    def no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    with pytest.raises(ToolError, match="is not available.*docker build -f ops/sandbox.Dockerfile"):
        _runner(FakeDocker(1, "", "No such image")).check_available()
    with pytest.raises(ToolError, match="Docker is not available"):
        ContainerTestRunner(IMAGE, runner=no_docker).check_available()


def test_a_sandbox_with_a_runner_never_runs_tests_on_the_host(tmp_path):
    (tmp_path / "marker.py").write_text("open('ran_on_host', 'w').write('x')\n", encoding="utf-8")
    fake = FakeDocker(0, "1 passed")
    sandbox = FileSandbox(tmp_path, ["src"], test_command="python marker.py", test_runner=_runner(fake))

    result = sandbox.run_tests()

    assert result.passed and len(fake.calls) == 1
    assert not (tmp_path / "ran_on_host").exists()


# --- settings: running model-written code on the host is an explicit choice ----------------------------------


def test_the_fixers_model_with_host_execution_needs_an_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("FIXER_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "k")

    with pytest.raises(ConfigError, match="runs tests of model-written code: set SANDBOX_EXEC=container"):
        validate_settings(load_settings())

    monkeypatch.setenv("SANDBOX_EXEC", "container")
    validate_settings(load_settings())
    monkeypatch.setenv("SANDBOX_EXEC", "host")
    monkeypatch.setenv("ALLOW_HOST_EXECUTION", "true")
    validate_settings(load_settings())


def test_without_the_fixers_model_host_execution_needs_no_opt_in():
    settings = load_settings()

    assert (settings.sandbox_exec, settings.allow_host_execution) == ("host", False)
    validate_settings(settings)


@pytest.mark.parametrize(
    "env,message",
    [
        ({"SANDBOX_EXEC": "vm"}, "SANDBOX_EXEC"),
        ({"SANDBOX_EXEC": "container", "SANDBOX_IMAGE": " "}, "requires SANDBOX_IMAGE"),
        ({"SANDBOX_PIDS_LIMIT": "4"}, "SANDBOX_PIDS_LIMIT"),
    ],
)
def test_bad_sandbox_settings_are_refused(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_the_app_will_not_start_in_container_mode_without_the_image(monkeypatch, tmp_path):
    from app.adapters.jenkins_client import MockJenkinsClient
    from app.services.change_control import ChangeControlStore
    from app.tools.factory import build_toolkit

    monkeypatch.setenv("SANDBOX_EXEC", "container")
    monkeypatch.setenv("SANDBOX_IMAGE", f"incidentflow-no-such-image-{uuid.uuid4().hex[:8]}:never")
    store = ChangeControlStore(str(tmp_path / "changes.json"))

    with pytest.raises(ToolError):  # Docker missing or image missing: either way it refuses to start
        build_toolkit(load_settings(), store, lambda: [], MockJenkinsClient(), tmp_path)


# --- the Verifier: a sandbox that cannot run is "inconclusive", not "the fix is wrong" ---------------------------


def test_when_the_container_cannot_run_the_fix_is_not_sent_back_but_handed_to_a_person(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    broken = _runner(FakeDocker(125, "", "Cannot connect to the Docker daemon"))
    opened = h.provider.open
    h.provider.open = lambda branch: _with_runner(opened(branch), broken)

    record = h.run()

    attempts = record.agent_run["attempts"]
    assert record.agent_run_status == "escalated" and len(attempts) == 1  # no pointless second attempt
    tests_check = next(c for c in attempts[0]["verification"]["checks"] if c["name"] == "tests")
    assert tests_check["status"] == "inconclusive" and "Docker daemon" in tests_check["detail"]
    assert "inconclusive" in record.agent_run["escalation_reason"]


def _with_runner(sandbox, runner):
    sandbox.test_runner = runner
    return sandbox


# --- real containers ---------------------------------------------------------------------------------------------


def _image_ready() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_docker = pytest.mark.skipif(not _image_ready(), reason=f"Docker or the {IMAGE} image is not available")

LOCKDOWN_TEST = '''
import os, socket, pytest

def test_the_worktree_cannot_be_changed():
    with pytest.raises(OSError):
        open("/work/written_by_test.txt", "w").write("x")

def test_the_root_filesystem_cannot_be_changed():
    with pytest.raises(OSError):
        open("/etc/written_by_test.txt", "w").write("x")

def test_there_is_no_network():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 80), timeout=3)

def test_no_secret_from_the_host():
    assert "LLM_API_KEY" not in os.environ and "SECRET_FROM_HOST" not in os.environ

def test_not_root():
    assert os.getuid() == 65534

def test_scratch_space_works(tmp_path):
    (tmp_path / "ok.txt").write_text("fine")
'''


@needs_docker
def test_a_real_container_runs_the_tests_and_enforces_the_lockdown(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("SECRET_FROM_HOST", "x")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lockdown.py").write_text(LOCKDOWN_TEST, encoding="utf-8")

    result = ContainerTestRunner(IMAGE, timeout_seconds=180).run(tmp_path)

    assert result.passed, result.output
    assert "6 passed" in result.output
    assert not (tmp_path / "written_by_test.txt").exists() and not (tmp_path / ".pytest_cache").exists()


@needs_docker
def test_a_real_failing_test_is_reported_as_failed(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")

    result = ContainerTestRunner(IMAGE, timeout_seconds=180).run(tmp_path)

    assert not result.passed and result.exit_code == 1 and "1 failed" in result.output


@needs_docker
def test_a_real_run_past_its_time_limit_is_stopped_and_leaves_no_container(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(120)\n", encoding="utf-8")
    runner = ContainerTestRunner(IMAGE, timeout_seconds=8)

    result = runner.run(tmp_path)

    assert not result.passed and "timed out after 8s" in result.output
    left = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=incidentflow-tests-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    assert left == ""


@needs_docker
def test_a_whole_agent_run_verifies_the_fix_with_tests_in_a_container(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    runner = ContainerTestRunner(IMAGE, command="python run_tests.py", timeout_seconds=180)
    opened = h.provider.open
    h.provider.open = lambda branch: _with_runner(opened(branch), runner)

    record = h.run()

    assert record.agent_run_status == "ready_for_approval"
    tests_check = next(c for c in record.agent_run["verification"]["checks"] if c["name"] == "tests")
    assert tests_check["status"] == "passed"


@needs_docker
def test_a_real_image_that_does_not_exist_fails_the_startup_check():
    with pytest.raises(ToolError, match="is not available"):
        ContainerTestRunner(f"incidentflow-no-such-image-{uuid.uuid4().hex[:8]}:never").check_available()
    ContainerTestRunner(IMAGE).check_available()  # the real one passes
