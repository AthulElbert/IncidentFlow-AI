import re
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
class PRPreparationResult:
    pr: PullRequestDraft
    local_branch_created: bool
    local_branch_message: str
    patch_artifact_path: str
    patch_preview: str
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
    ) -> None:
        self.pr_client = pr_client
        self.test_mode = test_mode.strip().lower()
        self.test_command = test_command.strip()
        self.repo_root = Path(repo_root)
        self.timeout_seconds = timeout_seconds
        self.base_branch = base_branch
        self.local_branch_mode = local_branch_mode.strip().lower()
        self.patch_output_dir = patch_output_dir.strip() or "generated_patches"

    def prepare(self, record: ChangeRecord, requested_by: str, comment: str = "") -> PRPreparationResult:
        title = f"[{record.change_id}] {record.summary[:72]}"
        branch = f"agent/{record.change_id.lower()}-{_slug(record.service)}"
        test_status, test_output, pass_rate = self._collect_test_evidence()
        patch_artifact_path, patch_preview = self._generate_patch_artifact(record, requested_by, comment)
        local_branch_created, local_branch_message = self._ensure_local_branch(branch)

        summary_lines = [
            f"Issue type: {record.issue_type} (confidence={record.confidence:.2f})",
            f"Jira: {record.jira_key}",
            f"Patch artifact: {patch_artifact_path}",
            f"Local branch: {branch} ({local_branch_message})",
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
            f"Test evidence status: {test_status}\n"
            f"Test command: {self.test_command}\n"
            f"Test output:\n{test_output}\n"
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
            test_evidence_status=test_status,
            test_command=self.test_command,
            test_output=test_output,
            test_pass_rate=pass_rate,
            pr_summary=pr_summary,
        )

    def _collect_test_evidence(self) -> tuple[str, str, float]:
        if self.test_mode != "pytest":
            return "passed", "Mock test evidence: simulated test pipeline passed.", 1.0

        cmd = self.test_command.split()
        proc = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
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

    def _issue_specific_template_lines(self, issue_type: str) -> list[str]:
        if issue_type == "performance_degradation":
            return [
                "+perf_tuning:",
                "+  connection_pool_max: 80",
                "+  request_timeout_ms: 2200",
                "+  cache:",
                "+    enabled: true",
                "+    ttl_seconds: 60",
            ]
        if issue_type == "application_error":
            return [
                "+error_guardrails:",
                "+  feature_flag_fallback: true",
                "+  retry:",
                "+    max_attempts: 2",
                "+    backoff_ms: 150",
                "+  circuit_breaker:",
                "+    failure_threshold: 5",
                "+    reset_timeout_seconds: 30",
            ]
        if issue_type == "dependency_failure":
            return [
                "+dependency_protection:",
                "+  upstream_timeout_ms: 1200",
                "+  fallback_response_mode: \"cached-or-default\"",
                "+  pool:",
                "+    max_inflight: 40",
                "+    queue_limit: 200",
            ]
        if issue_type == "resource_saturation":
            return [
                "+resource_controls:",
                "+  autoscaling:",
                "+    min_replicas: 4",
                "+    max_replicas: 12",
                "+  cpu_target_utilization_pct: 65",
                "+  memory_target_utilization_pct: 70",
            ]
        return [
            "+manual_triage:",
            "+  required: true",
            "+  reason: \"unknown issue type; collect deeper diagnostic evidence\"",
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
