"""The Verifier: independent evidence that a proposed fix works, and does not break anything else.

It is deliberately not model-driven. Its value is that it trusts neither the Fixer nor a model: it
recomputes what changed from the worktree, checks that against what was claimed, runs the tests,
deploys the branch to dev and measures. It never reads the Fixer's rationale.

Checks run cheapest first and each stage only runs if every required check before it passed, so a
change that is out of scope or does not parse is never deployed. Missing data is `inconclusive`,
never a pass.
"""
import ast
import json
import math
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.agents.collector import window_for
from app.agents.evidence import analyze_series, clean_text
from app.agents.models import Check, FixProposal, IncidentContext, VerificationReport
from app.paths import normalize_rel_path
from app.tools.base import TimeWindow, ToolBudgetExceeded, ToolError, ToolNotPermitted
from app.tools.instrumentation import AgentTools
from app.tools.sandbox import is_protected_path, is_under

MAX_SIDE_EFFECT_METRICS = 6
SIDE_EFFECT_RATIO = 1.5
MIN_DEV_POINTS = 3
_TERMINAL_FAILURES = {"FAILURE", "ABORTED", "UNKNOWN"}


def changed_paths(diff: str) -> list[str]:
    """Paths touched by a unified diff, from git ('diff --git') or plain ('+++ b/') headers."""
    paths: list[str] = []
    for line in diff.splitlines():
        match = re.match(r"diff --git a/(.+?) b/(.+)$", line) or re.match(r"\+\+\+ b/(.+)$", line)
        if match:
            path = normalize_rel_path(match.groups()[-1].strip())
            if path and path not in paths:
                paths.append(path)
    return paths


def changed_line_count(diff: str) -> int:
    return sum(
        1
        for line in diff.splitlines()
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---"))
    )


def _yaml_loader():
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None
    return yaml.safe_load


class Verifier:
    def __init__(
        self,
        allowed_paths: list[str],
        max_changed_lines: int = 200,
        deploy_wait_seconds: float = 120.0,
        poll_interval_seconds: float = 5.0,
        soak_minutes: float = 10.0,
        min_improvement: float = 0.5,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.allowed_paths = list(allowed_paths)
        self.max_changed_lines = max_changed_lines
        self.max_polls = max(1, math.ceil(deploy_wait_seconds / max(poll_interval_seconds, 0.001)))
        self.poll_interval_seconds = poll_interval_seconds
        self.soak_minutes = soak_minutes
        self.min_improvement = min_improvement
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep

    # ------------------------------------------------------------------ public

    def verify(
        self,
        ctx: IncidentContext,
        proposal: FixProposal,
        tools: AgentTools,
        issue_type: str = "unknown",
    ) -> VerificationReport:
        if "sandbox" not in tools.names:
            raise ValueError("the Verifier needs a sandbox for the fix under test")

        report = VerificationReport(incident_id=ctx.incident_id, branch=proposal.branch, verdict="inconclusive")
        # (check names the stage owns, how to run it). Stages run in order; each only runs if every
        # required check before it passed.
        stages: list[tuple[tuple[str, ...], Callable[[], Check | list[Check]]]] = [
            (("diff_present", "claimed_files_match", "scope"), lambda: self._check_diff(report, proposal, tools)),
            (("syntax",), lambda: self._check_syntax(report, tools)),
            (("tests",), lambda: self._check_tests(tools)),
            (("dev_deploy",), lambda: self._check_deploy(report, ctx, proposal, tools, issue_type)),
            (("alert_metric_recovered",), lambda: self._check_alert_metric(report, ctx, tools)),
            (("no_side_effects",), lambda: self._check_side_effects(report, ctx, tools)),
        ]

        blocked = ""
        for names, run in stages:
            produced: list[Check] = []
            if not blocked:
                try:
                    result = run()
                    produced = result if isinstance(result, list) else [result]
                except ToolBudgetExceeded as exc:
                    # Out of tool calls is "could not finish checking", never a pass and never a crash.
                    report.warnings.append(str(exc))
                    produced = [Check(name=names[0], status="inconclusive", detail="the tool-call budget ran out before this check finished")]
            report.checks.extend(produced)
            for name in names:  # anything the stage did not produce was not attempted
                if not any(check.name == name for check in report.checks):
                    report.checks.append(Check(name=name, status="skipped", detail=blocked or "not attempted"))
            if not blocked:
                failing = next((c for c in produced if c.required and c.status in {"failed", "inconclusive"}), None)
                if failing:
                    blocked = f"not attempted: '{failing.name}' did not pass"

        report.verdict = self._verdict(report.checks)
        report.feedback_for_fixer = self._feedback(report.checks)
        report.tool_calls_used = tools.calls_used
        return report

    # ------------------------------------------------------------------ stages

    def _check_diff(self, report: VerificationReport, proposal: FixProposal, tools: AgentTools) -> list[Check]:
        checks: list[Check] = []
        try:
            diff = tools.sandbox.diff()
        except ToolError as exc:
            return [
                Check(name="diff_present", status="inconclusive", detail=f"could not read the diff: {clean_text(str(exc), 160)}"),
            ]

        paths = changed_paths(diff)
        report.changed_files = paths
        if not diff.strip() or not paths:
            return [Check(name="diff_present", status="failed", detail="the worktree has no changes to verify")]
        checks.append(Check(name="diff_present", status="passed", detail=f"{len(paths)} file(s) changed", data={"files": paths}))

        claimed = sorted({normalize_rel_path(item) for item in proposal.claimed_files})
        actual = sorted(paths)
        if claimed != actual:
            checks.append(
                Check(
                    name="claimed_files_match",
                    status="failed",
                    detail=f"the Fixer claimed {claimed} but the worktree actually changed {actual}",
                    data={"claimed": claimed, "actual": actual},
                )
            )
        else:
            checks.append(Check(name="claimed_files_match", status="passed", detail="claimed and actual changes agree"))

        violations = [
            f"{path} ({'protected file' if is_protected_path(path) else 'outside the editable paths'})"
            for path in paths
            if is_protected_path(path) or not is_under(path, self.allowed_paths)
        ]
        lines = changed_line_count(diff)
        if lines > self.max_changed_lines:
            violations.append(f"{lines} changed lines exceeds the limit of {self.max_changed_lines}")
        if violations:
            checks.append(Check(name="scope", status="failed", detail="; ".join(violations), data={"violations": violations}))
        else:
            checks.append(Check(name="scope", status="passed", detail=f"{lines} changed line(s), all within the editable paths"))
        return checks

    def _check_syntax(self, report: VerificationReport, tools: AgentTools) -> Check:
        problems: list[str] = []
        checked = 0
        yaml_load = _yaml_loader()
        for path in report.changed_files:
            suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if suffix not in {"json", "py", "yaml", "yml"}:
                continue
            try:
                text = tools.sandbox.read(path)
            except ToolError:
                continue  # deleted or unreadable: nothing to parse
            try:
                if suffix == "json":
                    json.loads(text)
                elif suffix == "py":
                    ast.parse(text)
                elif yaml_load is not None:
                    yaml_load(text)
                else:
                    continue
                checked += 1
            except Exception as exc:
                problems.append(f"{path}: {clean_text(str(exc), 120)}")
        if problems:
            return Check(name="syntax", status="failed", detail="; ".join(problems), data={"problems": problems})
        return Check(name="syntax", status="passed", detail=f"{checked} file(s) parse cleanly" if checked else "no parseable files changed")

    def _check_tests(self, tools: AgentTools) -> Check:
        try:
            result = tools.sandbox.run_tests()
        except ToolError as exc:
            return Check(name="tests", status="inconclusive", detail=f"tests could not run: {clean_text(str(exc), 160)}")
        data = {"exit_code": result.exit_code, "duration_seconds": result.duration_seconds}
        if result.passed:
            return Check(name="tests", status="passed", detail="tests passed", data=data)
        return Check(name="tests", status="failed", detail=f"tests failed (exit {result.exit_code}): {clean_text(result.output[-300:], 300)}", data=data)

    def _check_deploy(
        self, report: VerificationReport, ctx: IncidentContext, proposal: FixProposal, tools: AgentTools, issue_type: str
    ) -> Check:
        try:
            started = tools.deploy_target.deploy_dev(ctx.service, proposal.branch, issue_type)
        except ToolError as exc:
            return Check(name="dev_deploy", status="inconclusive", detail=f"dev deploy could not start: {clean_text(str(exc), 160)}")
        report.deploy_url = started.url
        status = started.status.upper()
        try:
            for attempt in range(self.max_polls):
                if status not in {"QUEUED", "RUNNING", "IN_PROGRESS"}:
                    break
                if attempt:
                    self.sleep(self.poll_interval_seconds)
                status = tools.deploy_target.get_status(started.url).status.upper()
        except ToolError as exc:
            return Check(name="dev_deploy", status="inconclusive", detail=f"could not read the deploy status: {clean_text(str(exc), 160)}")

        if status == "SUCCESS":
            return Check(name="dev_deploy", status="passed", detail="deployed to dev", data={"url": started.url})
        if status in _TERMINAL_FAILURES:
            return Check(name="dev_deploy", status="failed", detail=f"dev deploy ended {status}", data={"url": started.url})
        return Check(name="dev_deploy", status="inconclusive", detail=f"dev deploy still {status} after {self.max_polls} polls", data={"url": started.url})

    def _check_alert_metric(self, report: VerificationReport, ctx: IncidentContext, tools: AgentTools) -> Check:
        name = "alert_metric_recovered"
        try:
            before_series = tools.metrics.query(ctx.service, ctx.alert_metric, window_for(ctx, 60))
        except ToolError as exc:
            return Check(name=name, status="inconclusive", detail=f"production metric unavailable: {clean_text(str(exc), 160)}")
        before = analyze_series(before_series)
        if not before.enough_data or not before.anomalous:
            return Check(
                name=name,
                status="inconclusive",
                detail=f"the incident is not visible in {ctx.alert_metric}, so there is no 'before' level to compare with",
            )

        try:
            dev_series = self._dev_series(ctx, ctx.alert_metric, tools)
        except (ToolError, ToolNotPermitted) as exc:
            return Check(name=name, status="inconclusive", detail=f"dev metric unavailable: {clean_text(str(exc), 160)}")
        if len(dev_series.points) < MIN_DEV_POINTS:
            return Check(name=name, status="inconclusive", detail=f"only {len(dev_series.points)} dev data point(s) for {ctx.alert_metric}")

        after = statistics.median(point.value for point in dev_series.points)
        improvement = (before.recent - after) / before.recent if before.recent > 0 else 0.0
        numbers = {
            "before": round(before.recent, 4),
            "after": round(after, 4),
            "threshold": ctx.threshold,
            "improvement": round(improvement, 3),
        }
        report.alert_metric = numbers

        problems = []
        if after > ctx.threshold:
            problems.append(f"{ctx.alert_metric} is {after:.4g} in dev, still above the alert threshold {ctx.threshold:.4g}")
        if improvement < self.min_improvement:
            problems.append(f"it improved only {improvement:.0%} (from {before.recent:.4g}); at least {self.min_improvement:.0%} is required")
        if problems:
            return Check(name=name, status="failed", detail="; ".join(problems), data=numbers)
        return Check(
            name=name,
            status="passed",
            detail=f"{ctx.alert_metric} fell from {before.recent:.4g} to {after:.4g} (threshold {ctx.threshold:.4g})",
            data=numbers,
        )

    def _check_side_effects(self, report: VerificationReport, ctx: IncidentContext, tools: AgentTools) -> Check:
        """Try to break it: no other metric of the service may be elevated against its own baseline."""
        name = "no_side_effects"
        try:
            names = [m for m in tools.dev_metrics.available_metrics(ctx.service) if m != ctx.alert_metric]
        except (ToolError, ToolNotPermitted) as exc:
            return Check(name=name, status="inconclusive", detail=f"could not list dev metrics: {clean_text(str(exc), 160)}")

        elevated: list[str] = []
        compared = 0
        for metric in names[:MAX_SIDE_EFFECT_METRICS]:
            try:
                prod = analyze_series(tools.metrics.query(ctx.service, metric, window_for(ctx, 60)))
                dev = self._dev_series(ctx, metric, tools)
            except (ToolError, ToolNotPermitted):
                continue
            if not prod.enough_data or len(dev.points) < MIN_DEV_POINTS:
                continue
            compared += 1
            after = statistics.median(point.value for point in dev.points)
            if prod.baseline > 0:
                worse = after >= prod.baseline * SIDE_EFFECT_RATIO
            else:  # a metric that is normally zero: anything sizeable compared with its incident level counts
                worse = prod.recent > 0 and after > 0.1 * prod.recent
            if worse:
                elevated.append(f"{metric} is {after:.4g} in dev (normal is about {prod.baseline:.4g})")

        if elevated:
            return Check(name=name, status="failed", detail="; ".join(elevated), data={"elevated": elevated})
        if names and not compared:
            return Check(name=name, status="inconclusive", detail="no other metric could be compared")
        return Check(name=name, status="passed", detail=f"{compared} other metric(s) are at their normal level" if compared else "the service has no other metrics")

    # ----------------------------------------------------------------- helpers

    def _dev_series(self, ctx: IncidentContext, metric: str, tools: AgentTools):
        now = self.clock()
        window = TimeWindow(start=now - timedelta(minutes=self.soak_minutes), end=now)
        return tools.dev_metrics.query(ctx.service, metric, window)

    @staticmethod
    def _verdict(checks: list[Check]) -> str:
        required = [check for check in checks if check.required]
        if any(check.status == "failed" for check in required):
            return "failed"
        if any(check.status in {"inconclusive", "skipped"} for check in required):
            return "inconclusive"
        return "passed"

    @staticmethod
    def _feedback(checks: list[Check]) -> str:
        lines = [f"- {check.name}: {check.detail}" for check in checks if check.status in {"failed", "inconclusive"}]
        return "\n".join(lines)
