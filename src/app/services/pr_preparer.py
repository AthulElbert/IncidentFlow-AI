import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.adapters.pr_client import PRClient
from app.models.schemas import ChangeRecord, PullRequestDraft


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-") or "service"


def _clip_text(value: str, max_chars: int = 4000) -> str:
    text = value.strip()
    if not text:
        return "No output captured."
    return text[-max_chars:]


@dataclass(frozen=True)
class CodeChangeResult:
    status: str
    message: str
    sandbox_worktree_path: str
    changed_files: list[str]
    commit_sha: str
    push_status: str
    test_evidence_status: str
    test_output: str
    test_pass_rate: float


@dataclass(frozen=True)
class PRPreparationResult:
    pr: PullRequestDraft
    local_branch_created: bool
    local_branch_message: str
    patch_artifact_path: str
    patch_preview: str
    code_change_status: str
    code_change_message: str
    sandbox_worktree_path: str
    changed_files: list[str]
    commit_sha: str
    push_status: str
    test_evidence_status: str
    test_command: str
    test_output: str
    test_pass_rate: float
    pr_summary: str


class PRPreparationService:
    def __init__(
        self,
        pr_client: PRClient,
        test_mode: str = "mock",
        test_command: str = "python -m pytest -q tests",
        repo_root: str = ".",
        timeout_seconds: int = 120,
        base_branch: str = "main",
        local_branch_mode: str = "spec",
        patch_output_dir: str = "generated_patches",
        code_change_mode: str = "spec",
        code_change_allowed_paths: str = "config/,runbooks/",
        code_change_max_lines: int = 200,
        code_change_auto_commit: bool = False,
        code_change_auto_push: bool = False,
    ) -> None:
        self.pr_client = pr_client
        self.test_mode = test_mode.strip().lower()
        self.test_command = test_command.strip()
        self.repo_root = Path(repo_root)
        self.timeout_seconds = timeout_seconds
        self.base_branch = base_branch
        self.local_branch_mode = local_branch_mode.strip().lower()
        self.patch_output_dir = patch_output_dir.strip() or "generated_patches"
        self.code_change_mode = code_change_mode.strip().lower()
        self.code_change_allowed_paths = [
            part.strip().replace("\\", "/")
            for part in code_change_allowed_paths.split(",")
            if part.strip()
        ]
        self.code_change_max_lines = max(10, code_change_max_lines)
        self.code_change_auto_commit = code_change_auto_commit
        self.code_change_auto_push = code_change_auto_push

    def prepare(self, record: ChangeRecord, requested_by: str, comment: str = "") -> PRPreparationResult:
        title = f"[{record.change_id}] {record.summary[:72]}"
        branch = f"agent/{record.change_id.lower()}-{_slug(record.service)}"
        patch_artifact_path, patch_preview = self._generate_patch_artifact(record, requested_by, comment)
        local_branch_created, local_branch_message = self._ensure_local_branch(branch)
        code_change = self._apply_code_change(record, branch, comment)

        summary_lines = [
            f"Issue type: {record.issue_type} (confidence={record.confidence:.2f})",
            f"Jira: {record.jira_key}",
            f"Patch artifact: {patch_artifact_path}",
            f"Local branch: {branch} ({local_branch_message})",
            f"Code change: {code_change.status} ({code_change.message})",
            "Proposed actions:",
        ]
        summary_lines.extend([f"- {item}" for item in record.proposed_actions])
        if record.triage_hypothesis_steps:
            summary_lines.append("Hypothesis steps:")
            summary_lines.extend([f"- {item}" for item in record.triage_hypothesis_steps])
        if comment:
            summary_lines.append(f"Reviewer note: {comment}")
        pr_summary = "\n".join(summary_lines)

        body = (
            f"Automated draft PR for change {record.change_id}\n\n"
            f"Requested by: {requested_by}\n\n"
            f"{pr_summary}\n\n"
            f"Test evidence status: {code_change.test_evidence_status}\n"
            f"Test command: {self.test_command}\n"
            f"Test output:\n{code_change.test_output}\n"
        )
        pr = self.pr_client.create_draft_pr(
            title=title,
            branch=branch,
            body=body,
            base_branch=self.base_branch,
        )
        return PRPreparationResult(
            pr=pr,
            local_branch_created=local_branch_created,
            local_branch_message=local_branch_message,
            patch_artifact_path=patch_artifact_path,
            patch_preview=patch_preview,
            code_change_status=code_change.status,
            code_change_message=code_change.message,
            sandbox_worktree_path=code_change.sandbox_worktree_path,
            changed_files=code_change.changed_files,
            commit_sha=code_change.commit_sha,
            push_status=code_change.push_status,
            test_evidence_status=code_change.test_evidence_status,
            test_command=self.test_command,
            test_output=code_change.test_output,
            test_pass_rate=code_change.test_pass_rate,
            pr_summary=pr_summary,
        )

    def _apply_code_change(self, record: ChangeRecord, branch: str, comment: str) -> CodeChangeResult:
        if self.code_change_mode != "sandbox_git":
            status, output, rate = self._collect_test_evidence(cwd=self.repo_root)
            return CodeChangeResult(
                status="not_started",
                message="spec-only (sandbox code change disabled)",
                sandbox_worktree_path="",
                changed_files=[],
                commit_sha="",
                push_status="not_attempted",
                test_evidence_status=status,
                test_output=output,
                test_pass_rate=rate,
            )

        is_git, reason = self._ensure_git_repo(self.repo_root)
        if not is_git:
            return CodeChangeResult(
                status="failed",
                message=f"cannot initialize sandbox worktree: {reason}",
                sandbox_worktree_path="",
                changed_files=[],
                commit_sha="",
                push_status="not_attempted",
                test_evidence_status="failed",
                test_output="Sandbox setup failed before tests.",
                test_pass_rate=0.0,
            )

        worktree_path = self.repo_root / ".agent_worktrees" / _slug(branch)
        ok, branch_msg = self._ensure_sandbox_worktree(branch, worktree_path)
        if not ok:
            return CodeChangeResult(
                status="failed",
                message=branch_msg,
                sandbox_worktree_path=str(worktree_path),
                changed_files=[],
                commit_sha="",
                push_status="not_attempted",
                test_evidence_status="failed",
                test_output="Sandbox setup failed before tests.",
                test_pass_rate=0.0,
            )

        rel_file = "config/service-overrides.yaml"
        allowed, rule_msg = self._validate_allowed_path(rel_file)
        if not allowed:
            return CodeChangeResult(
                status="failed",
                message=rule_msg,
                sandbox_worktree_path=str(worktree_path),
                changed_files=[],
                commit_sha="",
                push_status="not_attempted",
                test_evidence_status="failed",
                test_output="Guardrail rejected file path.",
                test_pass_rate=0.0,
            )

        target = worktree_path / rel_file
        target.parent.mkdir(parents=True, exist_ok=True)
        change_content = self._build_live_change_content(record, comment)
        line_count = len(change_content.splitlines())
        if line_count > self.code_change_max_lines:
            return CodeChangeResult(
                status="failed",
                message=f"guardrail rejected patch size {line_count} > {self.code_change_max_lines}",
                sandbox_worktree_path=str(worktree_path),
                changed_files=[],
                commit_sha="",
                push_status="not_attempted",
                test_evidence_status="failed",
                test_output="Guardrail rejected line count.",
                test_pass_rate=0.0,
            )

        target.write_text(change_content, encoding="utf-8")
        test_status, test_output, pass_rate = self._collect_test_evidence(cwd=worktree_path)
        commit_sha = ""
        push_status = "not_attempted"
        status = "applied"
        message = f"sandbox file updated ({rel_file}); {branch_msg}"

        if self.code_change_auto_commit:
            ok_add, add_out = self._run_git(["add", rel_file], cwd=worktree_path)
            if not ok_add:
                return CodeChangeResult(
                    status="failed",
                    message=f"git add failed: {add_out}",
                    sandbox_worktree_path=str(worktree_path),
                    changed_files=[rel_file],
                    commit_sha="",
                    push_status="not_attempted",
                    test_evidence_status=test_status,
                    test_output=test_output,
                    test_pass_rate=pass_rate,
                )

            commit_msg = f"agent: apply config mitigation for {record.change_id}"
            ok_commit, commit_out = self._run_git(["commit", "-m", commit_msg], cwd=worktree_path)
            if ok_commit:
                ok_sha, sha_out = self._run_git(["rev-parse", "--short", "HEAD"], cwd=worktree_path)
                commit_sha = sha_out.strip() if ok_sha else ""
            else:
                if "nothing to commit" not in commit_out.lower():
                    return CodeChangeResult(
                        status="failed",
                        message=f"git commit failed: {commit_out}",
                        sandbox_worktree_path=str(worktree_path),
                        changed_files=[rel_file],
                        commit_sha="",
                        push_status="not_attempted",
                        test_evidence_status=test_status,
                        test_output=test_output,
                        test_pass_rate=pass_rate,
                    )

            if self.code_change_auto_push:
                ok_push, push_out = self._run_git(["push", "-u", "origin", branch], cwd=worktree_path)
                push_status = "pushed" if ok_push else f"push_failed: {push_out}"
            else:
                push_status = "not_attempted"

        return CodeChangeResult(
            status=status,
            message=message,
            sandbox_worktree_path=str(worktree_path),
            changed_files=[rel_file],
            commit_sha=commit_sha,
            push_status=push_status,
            test_evidence_status=test_status,
            test_output=test_output,
            test_pass_rate=pass_rate,
        )

    def _collect_test_evidence(self, cwd: Path) -> tuple[str, str, float]:
        if self.test_mode != "pytest":
            return "passed", "Mock test evidence: simulated test pipeline passed.", 1.0

        cmd = shlex.split(self.test_command)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        clipped_output = _clip_text(output)
        passed = proc.returncode == 0
        pass_rate = 1.0 if passed else 0.0
        return ("passed" if passed else "failed"), clipped_output, pass_rate

    def _generate_patch_artifact(self, record: ChangeRecord, requested_by: str, comment: str) -> tuple[str, str]:
        output_dir = self.repo_root / self.patch_output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        patch_path = output_dir / f"{record.change_id}.patch"

        suggested_patch = self._build_patch_template(record, requested_by, comment)
        patch_path.write_text(suggested_patch, encoding="utf-8")
        return str(patch_path), _clip_text(suggested_patch, max_chars=1000)

    def _build_patch_template(self, record: ChangeRecord, requested_by: str, comment: str) -> str:
        lines = [
            f"# Change ID: {record.change_id}",
            f"# Service: {record.service}",
            f"# Requested by: {requested_by}",
            f"# Issue type: {record.issue_type}",
            f"# Confidence: {record.confidence:.2f}",
            f"# Jira: {record.jira_key}",
            "",
            "diff --git a/config/service-overrides.yaml b/config/service-overrides.yaml",
            "new file mode 100644",
            "index 0000000..1111111",
            "--- /dev/null",
            "+++ b/config/service-overrides.yaml",
            "@@",
            f"+service: {record.service}",
            "+environment: prod",
            "+change:",
            f"+  id: {record.change_id}",
            f"+  summary: \"{record.summary}\"",
            "+issue_type: " + record.issue_type,
            "+mitigation:",
            "+  strategy: \"auto-generated-template\"",
            "+  actions:",
        ]
        for action in record.proposed_actions:
            lines.append(f"+    - \"{action}\"")

        lines.extend(self._issue_specific_template_lines(record.issue_type))
        lines.extend(
            [
                "+validation:",
                "+  required_checks:",
                "+    - unit-tests",
                "+    - smoke-tests",
                "+  rollback_plan: \"Revert service-overrides.yaml and re-run deploy pipeline\"",
            ]
        )
        if comment:
            lines.append(f"+reviewer_note: \"{comment}\"")
        return "\n".join(lines) + "\n"

    def _build_live_change_content(self, record: ChangeRecord, comment: str) -> str:
        lines = [
            f"service: {record.service}",
            "environment: prod",
            "change:",
            f"  id: {record.change_id}",
            f"  summary: \"{record.summary}\"",
            f"issue_type: {record.issue_type}",
            "mitigation:",
            "  strategy: \"sandbox-git-applied\"",
            "  actions:",
        ]
        for item in record.proposed_actions:
            lines.append(f"    - \"{item}\"")
        lines.extend(self._issue_specific_template_lines(record.issue_type, yaml=True))
        lines.extend(
            [
                "validation:",
                "  required_checks:",
                "    - unit-tests",
                "    - smoke-tests",
            ]
        )
        if comment:
            lines.append(f"reviewer_note: \"{comment}\"")
        return "\n".join(lines) + "\n"

    def _issue_specific_template_lines(self, issue_type: str, yaml: bool = False) -> list[str]:
        prefix = "" if yaml else "+"
        if issue_type == "performance_degradation":
            return [
                f"{prefix}perf_tuning:",
                f"{prefix}  connection_pool_max: 80",
                f"{prefix}  request_timeout_ms: 2200",
                f"{prefix}  cache:",
                f"{prefix}    enabled: true",
                f"{prefix}    ttl_seconds: 60",
            ]
        if issue_type == "application_error":
            return [
                f"{prefix}error_guardrails:",
                f"{prefix}  feature_flag_fallback: true",
                f"{prefix}  retry:",
                f"{prefix}    max_attempts: 2",
                f"{prefix}    backoff_ms: 150",
                f"{prefix}  circuit_breaker:",
                f"{prefix}    failure_threshold: 5",
                f"{prefix}    reset_timeout_seconds: 30",
            ]
        if issue_type == "dependency_failure":
            return [
                f"{prefix}dependency_protection:",
                f"{prefix}  upstream_timeout_ms: 1200",
                f"{prefix}  fallback_response_mode: \"cached-or-default\"",
                f"{prefix}  pool:",
                f"{prefix}    max_inflight: 40",
                f"{prefix}    queue_limit: 200",
            ]
        if issue_type == "resource_saturation":
            return [
                f"{prefix}resource_controls:",
                f"{prefix}  autoscaling:",
                f"{prefix}    min_replicas: 4",
                f"{prefix}    max_replicas: 12",
                f"{prefix}  cpu_target_utilization_pct: 65",
                f"{prefix}  memory_target_utilization_pct: 70",
            ]
        return [
            f"{prefix}manual_triage:",
            f"{prefix}  required: true",
            f"{prefix}  reason: \"unknown issue type; collect deeper diagnostic evidence\"",
        ]

    def _ensure_local_branch(self, branch: str) -> tuple[bool, str]:
        if self.local_branch_mode != "git":
            return False, "spec-only (git branch creation disabled)"

        try:
            inside = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if inside.returncode != 0:
                return False, "not a git worktree"

            existing = subprocess.run(
                ["git", "branch", "--list", branch],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if existing.returncode == 0 and existing.stdout.strip():
                return True, "already exists"

            create = subprocess.run(
                ["git", "branch", branch],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if create.returncode == 0:
                return True, "created"

            detail = _clip_text((create.stderr or create.stdout or "git branch failed"), max_chars=200)
            return False, f"failed: {detail}"
        except Exception as exc:
            return False, f"failed: {exc}"

    def _ensure_git_repo(self, cwd: Path) -> tuple[bool, str]:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and "true" in (proc.stdout or "").lower():
            return True, "ok"
        return False, _clip_text(proc.stderr or proc.stdout or "not a git repository", max_chars=120)

    def _ensure_sandbox_worktree(self, branch: str, worktree_path: Path) -> tuple[bool, str]:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "worktree", "add", "--force", "-B", branch, str(worktree_path)],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc.returncode == 0:
            return True, "sandbox worktree ready"
        return False, _clip_text(proc.stderr or proc.stdout or "git worktree add failed", max_chars=220)

    def _validate_allowed_path(self, rel_path: str) -> tuple[bool, str]:
        normalized = rel_path.replace("\\", "/").lstrip("./")
        if not self.code_change_allowed_paths:
            return False, "guardrail rejected: no allowed paths configured"
        for prefix in self.code_change_allowed_paths:
            clean = prefix.rstrip("/")
            if normalized == clean or normalized.startswith(clean + "/"):
                return True, "allowed"
        return False, f"guardrail rejected path '{normalized}' not in {self.code_change_allowed_paths}"

    def _run_git(self, args: list[str], cwd: Path) -> tuple[bool, str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0, _clip_text(proc.stdout or proc.stderr or "", max_chars=500)
