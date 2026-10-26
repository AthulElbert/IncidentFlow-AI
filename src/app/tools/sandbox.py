"""A confined view of a repo checkout for agents.

Every path an agent passes is resolved against the sandbox root and refused if it
escapes it (absolute paths, `..`, symlinks). Secrets and CI/deploy files can
neither be read nor edited, edits are limited to an allow-list and a size cap,
and tests run with a scrubbed environment so repo code never sees the platform's
API keys.
"""
import difflib
import fnmatch
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Protocol

from app.paths import normalize_rel_path
from app.tools.base import ToolError
from app.tools.models import EditResult, FileMatch, TestResult

_READ_DENY = (".git/*", ".env", ".env.*", "*/.env", "*/.env.*", "*.pem", "*.key", "*id_rsa*", "secrets/*", "*/secrets/*")
_EDIT_DENY = _READ_DENY + (
    ".github/*",
    "Jenkinsfile*",
    "*/Jenkinsfile*",
    "Dockerfile*",
    "*/Dockerfile*",
    "docker-compose*",
    "*/docker-compose*",
)
_MAX_READ_BYTES = 256 * 1024
_MAX_GREP_FILE_BYTES = 1024 * 1024
_MAX_OUTPUT_CHARS = 4000
_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SystemDrive",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "VIRTUAL_ENV",
)


class RepoSandbox(Protocol):
    def list_files(self, subdir: str = "") -> list[str]:
        ...

    def read(self, path: str) -> str:
        ...

    def grep(self, pattern: str, subdir: str = "", max_matches: int = 50) -> list[FileMatch]:
        ...

    def edit(self, path: str, content: str) -> EditResult:
        ...

    def diff(self) -> str:
        ...

    def run_tests(self) -> TestResult:
        ...


_normalize = normalize_rel_path


def _denied(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel.split("/")[-1], pattern) for pattern in patterns)


class FileSandbox:
    def __init__(
        self,
        root: str | Path,
        allowed_edit_paths: list[str],
        max_edit_lines: int = 200,
        test_command: str = "python -m pytest -q tests",
        test_timeout_seconds: int = 120,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"sandbox root does not exist: {root}")
        self.allowed_edit_paths = [_normalize(item).rstrip("/") for item in allowed_edit_paths if item.strip()]
        self.max_edit_lines = max(1, max_edit_lines)
        self.test_command = test_command
        self.test_timeout_seconds = test_timeout_seconds
        self.extra_env = dict(extra_env or {})
        self._originals: dict[str, str | None] = {}

    # ------------------------------------------------------------------ paths

    def _resolve(self, path: str) -> tuple[Path, str]:
        raw = path.replace("\\", "/").strip()
        if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise ToolError(f"path must be relative to the repo root: '{path}'")
        if ".." in raw.split("/"):
            raise ToolError(f"path escapes the sandbox: '{path}'")
        rel = _normalize(raw)
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError(f"path escapes the sandbox: '{path}'")
        return target, target.relative_to(self.root).as_posix() if target != self.root else ""

    def _inside(self, path: Path) -> bool:
        resolved = path.resolve()
        return resolved == self.root or self.root in resolved.parents

    def _iter_files(self, subdir: str = ""):
        base = self._resolve(subdir)[0] if subdir.strip() not in {"", "."} else self.root
        if not base.exists():
            raise ToolError(f"no such directory: '{subdir}'")
        for current, dirs, files in os.walk(base):
            # Check where each entry really lives: a symlink or Windows junction inside the
            # repo can point anywhere, and walking it would expose files outside the sandbox.
            dirs[:] = sorted(
                d
                for d in dirs
                if d != ".git" and not _denied(f"{d}/x", _READ_DENY) and self._inside(Path(current) / d)
            )
            for name in sorted(files):
                full = Path(current) / name
                rel = full.relative_to(self.root).as_posix()
                if _denied(rel, _READ_DENY) or not self._inside(full):
                    continue
                yield full, rel

    # ------------------------------------------------------------------- read

    def list_files(self, subdir: str = "") -> list[str]:
        return [rel for _, rel in self._iter_files(subdir)]

    def read(self, path: str) -> str:
        target, rel = self._resolve(path)
        if _denied(rel, _READ_DENY):
            raise ToolError(f"reading '{rel}' is not allowed")
        if not target.is_file():
            raise ToolError(f"no such file: '{rel}'")
        if target.stat().st_size > _MAX_READ_BYTES:
            raise ToolError(f"'{rel}' is larger than {_MAX_READ_BYTES // 1024} KB; use grep to find the part you need")
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"'{rel}' is not a UTF-8 text file") from exc

    def grep(self, pattern: str, subdir: str = "", max_matches: int = 50) -> list[FileMatch]:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid pattern: {exc}") from exc
        matches: list[FileMatch] = []
        limit = max(1, min(max_matches, 200))
        for full, rel in self._iter_files(subdir):
            if full.stat().st_size > _MAX_GREP_FILE_BYTES:
                continue
            try:
                text = full.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(FileMatch(path=rel, line=number, text=line.strip()[:300]))
                    if len(matches) >= limit:
                        return matches
        return matches

    # ------------------------------------------------------------------- edit

    def _check_editable(self, rel: str) -> None:
        if _denied(rel, _EDIT_DENY):
            raise ToolError(f"editing '{rel}' is not allowed")
        if not self.allowed_edit_paths:
            raise ToolError("no editable paths are configured")
        if not any(rel == prefix or rel.startswith(prefix + "/") for prefix in self.allowed_edit_paths):
            raise ToolError(f"'{rel}' is outside the editable paths {self.allowed_edit_paths}")

    def edit(self, path: str, content: str) -> EditResult:
        target, rel = self._resolve(path)
        if not rel:
            raise ToolError("path must name a file")
        self._check_editable(rel)
        lines = len(content.splitlines())
        if lines > self.max_edit_lines:
            raise ToolError(f"edit of {lines} lines exceeds the limit of {self.max_edit_lines}")
        if target.exists() and not target.is_file():
            raise ToolError(f"'{rel}' is not a file")

        existed = target.exists()
        original = target.read_text(encoding="utf-8") if existed else None
        self._originals.setdefault(rel, original)
        if original == content:
            return EditResult(path=rel, lines=lines, changed=False)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return EditResult(path=rel, lines=lines, changed=True)

    def diff(self) -> str:
        """Unified diff of every file edited through this sandbox against its original content."""
        chunks: list[str] = []
        for rel in sorted(self._originals):
            original = self._originals[rel]
            target = self.root / rel
            current = target.read_text(encoding="utf-8") if target.exists() else None
            if original == current:
                continue
            chunks.extend(
                difflib.unified_diff(
                    (original or "").splitlines(keepends=True),
                    (current or "").splitlines(keepends=True),
                    fromfile="/dev/null" if original is None else f"a/{rel}",
                    tofile="/dev/null" if current is None else f"b/{rel}",
                )
            )
        return "".join(chunks)

    # ------------------------------------------------------------------ tests

    def _test_env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST}
        env.update(self.extra_env)
        return env

    def run_tests(self) -> TestResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                shlex.split(self.test_command, posix=os.name != "nt"),
                cwd=str(self.root),
                env=self._test_env(),
                capture_output=True,
                text=True,
                timeout=self.test_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False,
                exit_code=-1,
                output=f"test run timed out after {self.test_timeout_seconds}s",
                duration_seconds=round(time.monotonic() - started, 2),
            )
        except OSError as exc:
            raise ToolError(f"could not run tests: {exc}") from exc

        output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return TestResult(
            passed=proc.returncode == 0,
            exit_code=proc.returncode,
            output=output[-_MAX_OUTPUT_CHARS:] or "No output captured.",
            duration_seconds=round(time.monotonic() - started, 2),
        )


class GitWorktreeProvider:
    """Opens a sandbox on an isolated `git worktree` for a branch."""

    def __init__(
        self,
        repo_root: str | Path,
        allowed_edit_paths: list[str],
        max_edit_lines: int = 200,
        test_command: str = "python -m pytest -q tests",
        test_timeout_seconds: int = 120,
        worktrees_dir: str = ".agent_worktrees",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.allowed_edit_paths = allowed_edit_paths
        self.max_edit_lines = max_edit_lines
        self.test_command = test_command
        self.test_timeout_seconds = test_timeout_seconds
        self.worktrees_dir = worktrees_dir

    def open(self, branch: str) -> FileSandbox:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", branch.strip().lower()).strip("-")
        if not slug:
            raise ToolError("branch name is required")
        path = self.repo_root / self.worktrees_dir / slug
        path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "worktree", "add", "--force", "-B", branch, str(path)],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc.returncode != 0:
            raise ToolError(f"could not create worktree: {(proc.stderr or proc.stdout).strip()[:300]}")
        return FileSandbox(
            path,
            allowed_edit_paths=self.allowed_edit_paths,
            max_edit_lines=self.max_edit_lines,
            test_command=self.test_command,
            test_timeout_seconds=self.test_timeout_seconds,
        )
