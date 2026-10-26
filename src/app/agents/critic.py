"""The Critic: looks for what automated verification could miss, and warns the human reviewer.

Like the Verifier it is code-first. The rules check that verification really passed, that the change
relates to what the evidence implicates (scope creep), and how risky the change is. An optional
model may add concerns; it can only make the review stricter, never approve something the rules did
not, and never override a failed check.

`approve_for_human` is not an approval: a person still decides. It means the Critic found nothing
that should stop the fix reaching them.
"""
import json
import logging
import re
from typing import Any

from app.adapters.llm_client import LLMClient
from app.agents.evidence import clean_text
from app.agents.models import (
    Concern,
    FixProposal,
    IncidentContext,
    InvestigationReport,
    Review,
    VerificationReport,
)
from app.agents.prompts import CRITIC_SYSTEM_PROMPT
from app.agents.verifier import changed_line_count, changed_paths
from app.tools.base import ToolError
from app.tools.instrumentation import AgentTools

logger = logging.getLogger("app.critic")

HIGH_RISK_PATH = re.compile(r"auth|payment|billing|migration|schema|security|secret|permission|iam|crypto", re.I)
CODE_SUFFIXES = {"py", "js", "ts", "java", "go", "rb", "rs", "cs", "php", "kt", "sql", "sh"}
THIN_CONFIDENCE = 0.70
THIN_MARGIN = 0.10  # recovery barely above the required improvement
MAX_DIFF_CHARS = 12_000
_ARTIFACT = re.compile(r"[\w./-]+\.(?:json|ya?ml|py|js|ts|java|go|rb|rs|cs|php|kt|sql|sh|toml|ini|conf|properties|cfg)\b", re.I)


def implicated_artifacts(investigation: InvestigationReport) -> set[str]:
    """File names and identifiers the evidence points at (deploy refs and summaries, quoted log lines)."""
    found: set[str] = set()
    for item in investigation.evidence:
        if item.kind not in {"deploy", "log_pattern", "similar_incident", "foreign_deploy"}:
            continue
        texts = [item.summary, str(item.data.get("ref", "")), str(item.data.get("change_summary", ""))]
        texts.extend(str(sample) for sample in item.data.get("samples", []))
        for text in texts:
            for match in _ARTIFACT.findall(text):
                found.add(match.lower())
    return found


def _risk(paths: list[str], lines: int) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if any(HIGH_RISK_PATH.search(path) for path in paths):
        reasons.append("touches a path that looks security, payment or schema related")
    if len(paths) > 5:
        reasons.append(f"changes {len(paths)} files")
    if lines > 150:
        reasons.append(f"changes {lines} lines")
    if reasons:
        return "high", reasons

    suffixes = {path.rsplit(".", 1)[-1].lower() for path in paths if "." in path}
    medium: list[str] = []
    if suffixes & CODE_SUFFIXES:
        medium.append("changes source code, not only configuration")
    if len(paths) > 2:
        medium.append(f"changes {len(paths)} files")
    if lines > 50:
        medium.append(f"changes {lines} lines")
    return ("medium", medium) if medium else ("low", [])


class Critic:
    def __init__(self, llm: LLMClient | None = None, min_improvement: float = 0.5) -> None:
        self.llm = llm
        self.min_improvement = min_improvement

    def review(
        self,
        ctx: IncidentContext,
        investigation: InvestigationReport,
        proposal: FixProposal,
        verification: VerificationReport,
        tools: AgentTools,
    ) -> Review:
        warnings: list[str] = []
        concerns: list[Concern] = []

        diff = ""
        try:
            diff = tools.sandbox.diff()
        except ToolError as exc:
            concerns.append(Concern(severity="blocking", code="diff_unreadable", text=f"could not read the diff: {clean_text(str(exc), 160)}"))
        paths = changed_paths(diff) or verification.changed_files
        lines = changed_line_count(diff)

        concerns.extend(self._verification_concerns(verification))
        concerns.extend(self._diagnosis_concerns(investigation))
        concerns.extend(self._scope_concerns(investigation, paths))
        risk, risk_reasons = _risk(paths, lines)
        if risk != "low":
            concerns.append(
                Concern(severity="info", code=f"{risk}_risk", text=f"{risk} risk: " + "; ".join(risk_reasons))
            )

        mode = "rules"
        if self.llm is not None:
            try:
                concerns.extend(self._llm_concerns(ctx, investigation, verification, diff))
                mode = "llm"
            except Exception as exc:  # advisory only: a failure never changes the outcome
                warnings.append(f"LLM review unavailable: {clean_text(str(exc), 200)}")

        decision = self._decision(concerns)
        feedback = "\n".join(
            f"- {c.text}" for c in concerns if c.severity in {"major", "blocking"}
        ) if decision != "approve_for_human" else ""
        return Review(
            incident_id=ctx.incident_id,
            decision=decision,
            risk=risk,
            concerns=concerns,
            changed_files=paths,
            changed_lines=lines,
            feedback_for_fixer=feedback,
            mode_used=mode,  # type: ignore[arg-type]
            warnings=warnings,
            tool_calls_used=tools.calls_used,
        )

    # --------------------------------------------------------------------- rules

    def _verification_concerns(self, verification: VerificationReport) -> list[Concern]:
        if verification.verdict != "passed":
            failing = ", ".join(c.name for c in verification.checks if c.status in {"failed", "inconclusive"}) or "none listed"
            return [
                Concern(
                    severity="blocking",
                    code="not_verified",
                    text=f"verification did not pass (verdict {verification.verdict}; problem checks: {failing})",
                )
            ]
        found: list[Concern] = []
        improvement = verification.alert_metric.get("improvement")
        if improvement is not None and improvement < self.min_improvement + THIN_MARGIN:
            found.append(
                Concern(
                    severity="minor",
                    code="thin_recovery",
                    text=f"the alert metric improved {improvement:.0%}, only just above the required {self.min_improvement:.0%}",
                )
            )
        return found

    @staticmethod
    def _diagnosis_concerns(investigation: InvestigationReport) -> list[Concern]:
        top = investigation.top
        if investigation.decision == "escalate" or top is None or top.issue_type == "unknown":
            return [
                Concern(
                    severity="blocking",
                    code="no_confident_diagnosis",
                    text="the investigation did not reach a confident diagnosis, so there is no cause for this fix to be fixing",
                )
            ]
        if top.confidence < THIN_CONFIDENCE:
            return [
                Concern(
                    severity="minor",
                    code="moderate_confidence",
                    text=f"the diagnosis ({top.issue_type}) has confidence {top.confidence:.2f}; a reviewer should confirm the cause",
                )
            ]
        return []

    @staticmethod
    def _scope_concerns(investigation: InvestigationReport, paths: list[str]) -> list[Concern]:
        implicated = implicated_artifacts(investigation)
        if not paths:
            return []
        if not implicated:
            return [
                Concern(
                    severity="info",
                    code="cannot_relate_change",
                    text="the evidence names no file, so the Critic cannot check that the change relates to the cause",
                )
            ]

        def related(path: str) -> bool:
            name = path.lower().rsplit("/", 1)[-1]
            return any(name == art.rsplit("/", 1)[-1] or path.lower() == art or art.endswith("/" + name) for art in implicated)

        unrelated = [path for path in paths if not related(path)]
        if len(unrelated) == len(paths):
            return [
                Concern(
                    severity="major",
                    code="change_not_implicated",
                    text=f"none of the changed files ({', '.join(paths)}) is named by the evidence; the evidence points at {sorted(implicated)[:4]}",
                )
            ]
        if unrelated:
            return [
                Concern(
                    severity="major",
                    code="scope_creep",
                    text=f"also changes {', '.join(unrelated)}, which no evidence implicates",
                )
            ]
        return []

    # ------------------------------------------------------------------ decision

    @staticmethod
    def _decision(concerns: list[Concern]) -> str:
        if any(c.severity == "blocking" for c in concerns):
            return "block"
        if any(c.severity == "major" for c in concerns):
            return "send_back"
        return "approve_for_human"

    # ----------------------------------------------------------------------- LLM

    def _llm_concerns(
        self, ctx: IncidentContext, investigation: InvestigationReport, verification: VerificationReport, diff: str
    ) -> list[Concern]:
        assert self.llm is not None
        top = investigation.top
        payload: dict[str, Any] = {
            "incident": {"service": ctx.service, "alert_metric": ctx.alert_metric, "alert_message": clean_text(ctx.alert_message, 300)},
            "diagnosis": {
                "issue_type": top.issue_type if top else "unknown",
                "root_cause": clean_text(top.root_cause, 500) if top else "",
            },
            "evidence": [{"id": e.id, "summary": e.summary} for e in investigation.evidence[:20]],
            "verification": {
                "verdict": verification.verdict,
                "checks": {c.name: c.status for c in verification.checks},
                "alert_metric": verification.alert_metric,
            },
            "diff": diff[:MAX_DIFF_CHARS],
        }
        reply = self.llm.complete_json(
            [
                {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            error_label="critic",
        )
        items = reply.get("concerns")
        if not isinstance(items, list) or len(items) > 10:
            raise ValueError("reply needs a 'concerns' list of at most 10 items")

        found: list[Concern] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("a concern is not an object")
            severity = str(item.get("severity", "")).strip().lower()
            text = clean_text(str(item.get("text", "")), 400)
            if severity not in {"minor", "major"}:  # a model may not raise a blocking concern
                raise ValueError(f"unsupported severity '{severity}'")
            if not text:
                raise ValueError("a concern has no text")
            found.append(Concern(severity=severity, code="llm_review", text=text, source="llm"))  # type: ignore[arg-type]
        return found
