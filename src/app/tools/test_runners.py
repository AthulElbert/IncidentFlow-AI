"""Where a proposed fix's tests run.

The tests of a worktree the Fixer edited execute code the Fixer may have written. `ContainerTestRunner` runs them
in a locked-down container instead of on the host:

- the worktree is mounted read-only, and the container's root filesystem is read-only; only a small `/tmp` is
  writable, so the tests can neither change the code under test nor leave anything behind;
- no network, not even DNS;
- every Linux capability dropped, no privilege escalation, an unprivileged user;
- memory, CPU and process limits, and a wall-clock timeout after which the container is removed;
- none of the host's environment (and so none of its secrets) is passed in.

A failure of Docker itself (not installed, daemon down, image missing, command not found in the image) raises
`ToolError`, which the Verifier reports as "tests could not run" (inconclusive). It is never reported as "the
tests failed", which would send the Fixer back to change code that is not the problem. It never falls back to
running on the host.
"""
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Protocol

from app.tools.base import ToolError
from app.tools.models import TestResult

# `docker run` exits 125 when Docker could not run the container, 126 when the command cannot be executed and
# 127 when it does not exist. None of these says anything about the code under test.
_DOCKER_FAILURE_CODES = frozenset({125, 126, 127})
_MAX_OUTPUT_CHARS = 6000


class TestRunner(Protocol):
    __test__ = False  # not a pytest class

    def run(self, root: Path) -> TestResult:
        ...


class ContainerTestRunner:
    __test__ = False

    def __init__(
        self,
        image: str,
        command: str = "python -m pytest -q tests",
        timeout_seconds: int = 120,
        memory: str = "1g",
        cpus: str = "1",
        pids_limit: int = 256,
        tmpfs_mb: int = 512,
        docker: str = "docker",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        if not image.strip():
            raise ValueError("a container image is required")
        self.image = image.strip()
        self.command = shlex.split(command)
        if not self.command:
            raise ValueError("a test command is required")
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.tmpfs_mb = tmpfs_mb
        self.docker = docker
        self._run = runner

    def docker_args(self, root: Path, name: str) -> list[str]:
        """The full `docker run` command. Public so the lockdown can be inspected and tested."""
        return [
            self.docker, "run", "--rm", "--name", name,
            "--network", "none",
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={self.tmpfs_mb}m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "--pids-limit", str(self.pids_limit),
            "--memory", self.memory,
            "--memory-swap", self.memory,  # no swap beyond the memory limit
            "--cpus", self.cpus,
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTEST_ADDOPTS=-p no:cacheprovider",
            "--volume", f"{Path(root).resolve()}:/work:ro",
            "--workdir", "/work",
            self.image,
            *self.command,
        ]

    def run(self, root: Path) -> TestResult:
        name = f"incidentflow-tests-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        try:
            proc = self._run(
                self.docker_args(root, name),
                capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired:
            self._remove(name)
            return TestResult(
                passed=False,
                exit_code=-1,
                output=f"test run timed out after {self.timeout_seconds}s (container removed)",
                duration_seconds=round(time.monotonic() - started, 2),
            )
        except OSError as exc:
            raise ToolError(f"could not start the test container: {exc}") from exc

        output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        if proc.returncode in _DOCKER_FAILURE_CODES:
            raise ToolError(f"the test container could not run (docker exit {proc.returncode}): {output[-400:]}")
        return TestResult(
            passed=proc.returncode == 0,
            exit_code=proc.returncode,
            output=output[-_MAX_OUTPUT_CHARS:] or "No output captured.",
            duration_seconds=round(time.monotonic() - started, 2),
        )

    def check_available(self) -> None:
        """Raise ToolError unless Docker is reachable and the image is present. Run at startup: fail closed."""
        try:
            proc = self._run(
                [self.docker, "image", "inspect", "--format", "{{.Id}}", self.image],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolError(f"Docker is not available: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise ToolError(
                f"the sandbox image '{self.image}' is not available ({detail}). "
                "Build it with: docker build -f ops/sandbox.Dockerfile -t incidentflow-sandbox:latest ."
            )

    def _remove(self, name: str) -> None:
        try:
            self._run([self.docker, "rm", "--force", name], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
