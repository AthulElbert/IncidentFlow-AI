"""The Investigator: explains an alert from evidence and decides whether to proceed or hand off.

Evidence is always gathered by the fixed plan in `collector`. Confidence always comes from
`scoring`. A model, when configured, adds two things only: a few extra evidence requests and the
wording of the root cause. If it fails or contradicts the evidence, the evidence-based answer wins.
"""
import json
import logging
from typing import Any

from app.adapters.llm_client import LLMClient
from app.agents.collector import collect_evidence, window_for
from app.agents.evidence import EvidenceLedger, clean_text
from app.agents.models import ISSUE_TYPES, Evidence, Hypothesis, IncidentContext, InvestigationReport
from app.agents.prompts import FINAL_ANSWER_INSTRUCTION, INVESTIGATOR_SYSTEM_PROMPT
from app.agents.scoring import Candidate, score_all
from app.tools.base import ToolError
from app.tools.instrumentation import AgentTools

logger = logging.getLogger("app.investigator")

MAX_HYPOTHESES = 3
MIN_LISTED_CONFIDENCE = 0.10
MAX_ROOT_CAUSE_CHARS = 500
_PREFIX = {
    "application_error": "Likely application regression",
    "performance_degradation": "Likely performance degradation",
    "dependency_failure": "Likely failing dependency",
    "resource_saturation": "Likely resource exhaustion",
}


def compose_root_cause(issue_type: str, supporting: list[Evidence], all_evidence: list[Evidence]) -> str:
    """Word a root cause using only phrases the evidence itself provided."""
    if issue_type == "unknown":
        anomalies = [item.phrase for item in all_evidence if item.kind == "metric_anomaly" and item.phrase]
        seen = f" Observed: {'; '.join(anomalies[:2])}." if anomalies else ""
        return f"Insufficient evidence to name a cause; manual triage needed.{seen}"

    deploys = [item.phrase for item in supporting if item.kind == "deploy" and item.phrase][:1]
    logs = sorted(
        (item for item in supporting if item.kind == "log_pattern" and item.phrase),
        key=lambda item: -int(item.data.get("count", 0)),
    )
    log_phrases = [item.phrase for item in logs[:2]]
    metrics = [item.phrase for item in supporting if item.kind == "metric_anomaly" and item.phrase][:2]
    similar = [
        f"similar past incident {item.data['incident_id']} was resolved by: {item.data['resolution']}"
        for item in supporting
        if item.kind == "similar_incident"
    ][:1]
    parts = deploys + log_phrases + metrics + similar
    text = f"{_PREFIX[issue_type]}: " + "; ".join(parts) if parts else f"{_PREFIX[issue_type]}."
    return clean_text(text, MAX_ROOT_CAUSE_CHARS)


class Investigator:
    def __init__(
        self,
        llm: LLMClient | None = None,
        min_confidence: float = 0.60,
        min_margin: float = 0.10,
        max_llm_steps: int = 3,
    ) -> None:
        self.llm = llm
        self.min_confidence = min_confidence
        self.min_margin = min_margin
        self.max_llm_steps = max(0, max_llm_steps)

    # ------------------------------------------------------------------ public

    def investigate(self, ctx: IncidentContext, tools: AgentTools) -> InvestigationReport:
        ledger = collect_evidence(ctx, tools)
        hypotheses = self._hypotheses(score_all(ledger.items), ledger)
        warnings = list(ledger.warnings)
        mode = "heuristic"

        if self.llm is not None:
            try:
                hypotheses = self._reason_with_llm(ctx, tools, ledger, warnings)
                mode = "llm"
            except Exception as exc:  # any model failure falls back to the evidence-based answer
                logger.warning("investigator-llm-fallback", extra={"incident_id": ctx.incident_id, "error": str(exc)})
                warnings.append(f"LLM investigation fallback: {clean_text(str(exc), 200)}")
                hypotheses = self._hypotheses(score_all(ledger.items), ledger)
            warnings.extend(w for w in ledger.warnings if w not in warnings)

        decision, reason = self._decide(hypotheses)
        return InvestigationReport(
            incident_id=ctx.incident_id,
            service=ctx.service,
            mode_used=mode,
            decision=decision,
            escalation_reason=reason,
            hypotheses=hypotheses,
            evidence=ledger.items,
            warnings=warnings,
            tool_calls_used=tools.calls_used,
        )

    # --------------------------------------------------------------- hypotheses

    def _hypotheses(self, candidates: dict[str, Candidate], ledger: EvidenceLedger) -> list[Hypothesis]:
        ranked = sorted(candidates.values(), key=lambda c: (-c.confidence, c.issue_type))
        chosen = [c for c in ranked if c.confidence >= MIN_LISTED_CONFIDENCE][:MAX_HYPOTHESES] or ranked[:1]
        return [self._hypothesis(rank, candidate, ledger) for rank, candidate in enumerate(chosen, start=1)]

    def _hypothesis(self, rank: int, candidate: Candidate, ledger: EvidenceLedger, root_cause: str = "", source: str = "evidence") -> Hypothesis:
        supporting = [ledger.get(i) for i in candidate.supporting]
        return Hypothesis(
            rank=rank,
            issue_type=candidate.issue_type,
            root_cause=root_cause or compose_root_cause(candidate.issue_type, [e for e in supporting if e], ledger.items),
            confidence=candidate.confidence,
            supporting=candidate.supporting,
            contradicting=candidate.contradicting,
            missing_signals=candidate.missing,
            source=source,  # type: ignore[arg-type]
        )

    # ----------------------------------------------------------------- decision

    def _decide(self, hypotheses: list[Hypothesis]) -> tuple[str, str]:
        top = hypotheses[0]
        second = hypotheses[1] if len(hypotheses) > 1 else None
        if top.issue_type == "unknown":
            return "escalate", "the evidence does not support a specific cause"
        if top.confidence < self.min_confidence:
            return "escalate", f"confidence {top.confidence:.2f} is below the minimum {self.min_confidence:.2f}"
        # With no listed runner-up the alternative is worth 0, so the margin is the confidence itself.
        runner_up = second.confidence if second else 0.0
        if top.confidence - runner_up < self.min_margin:
            alternative = f"{second.issue_type} ({second.confidence:.2f})" if second else "no alternative"
            return (
                "escalate",
                f"ambiguous: {top.issue_type} ({top.confidence:.2f}) and {alternative} "
                f"are within {self.min_margin:.2f} of each other",
            )
        return "proceed", ""

    # --------------------------------------------------------------------- LLM

    def _reason_with_llm(
        self, ctx: IncidentContext, tools: AgentTools, ledger: EvidenceLedger, warnings: list[str]
    ) -> list[Hypothesis]:
        assert self.llm is not None
        messages: list[dict] = [
            {"role": "system", "content": INVESTIGATOR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(self._prompt_payload(ctx, ledger, self.max_llm_steps))},
        ]
        requests_left = self.max_llm_steps

        for _ in range(self.max_llm_steps + 2):
            reply = self.llm.complete_json(messages, error_label="investigator")
            action = reply.get("action")

            if action == "final":
                return self._merge_llm_answer(reply, ledger, warnings)

            if action != "tool":
                raise ValueError(f"unexpected action '{action}'")

            messages.append({"role": "assistant", "content": json.dumps(reply)})
            if requests_left <= 0:
                messages.append({"role": "user", "content": json.dumps({"instruction": FINAL_ANSWER_INSTRUCTION})})
                continue

            requests_left -= 1
            observation = self._run_tool_request(reply, ctx, tools, ledger)
            payload = {"observation": observation, "tool_requests_remaining": requests_left}
            if requests_left <= 0:
                payload["instruction"] = FINAL_ANSWER_INSTRUCTION
            messages.append({"role": "user", "content": json.dumps(payload)})

        raise ValueError("model did not give a final answer")

    @staticmethod
    def _prompt_payload(ctx: IncidentContext, ledger: EvidenceLedger, steps: int) -> dict[str, Any]:
        return {
            "incident": {
                "service": ctx.service,
                "environment": ctx.environment,
                "alert_metric": ctx.alert_metric,
                "alert_message": clean_text(ctx.alert_message, 300),
                "value": ctx.value,
                "threshold": ctx.threshold,
                "occurred_at": ctx.occurred_at.isoformat(),
            },
            "evidence": [_evidence_for_prompt(item) for item in ledger.items],
            "tool_requests_remaining": steps,
        }

    def _run_tool_request(
        self, request: dict[str, Any], ctx: IncidentContext, tools: AgentTools, ledger: EvidenceLedger
    ) -> dict[str, Any]:
        tool, method = request.get("tool"), request.get("method")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        service = clean_text(str(args.get("service") or ctx.service), 80)
        try:
            minutes = min(240.0, max(5.0, float(args.get("minutes", 60))))
        except (TypeError, ValueError):
            minutes = 60.0
        window = window_for(ctx, minutes)

        try:
            if (tool, method) == ("logs", "search"):
                query = clean_text(str(args.get("query", "")), 100)
                lines = tools.logs.search(service, query, window, limit=50)
                new = ledger.add_logs(lines, source=f"logs.search({service}, {query!r})", service=service)
                quotes = [clean_text(line.message, 160) for line in lines[-10:]]
                return {"new_evidence": [_evidence_for_prompt(e) for e in new], "log_lines_returned": len(lines), "quotes": quotes}
            if (tool, method) == ("metrics", "query"):
                metric = clean_text(str(args.get("metric", "")), 80)
                series = tools.metrics.query(service, metric, window)
                added = ledger.add_metric(series, source=f"metrics.query({service}, {metric})")
                return {"new_evidence": [_evidence_for_prompt(added)] if added else [], "points": len(series.points)}
            if (tool, method) == ("deploys", "recent"):
                deploys = tools.deploys.recent(service, window)
                new = ledger.add_deploys(deploys, int(minutes), source=f"deploys.recent({service})", service=service)
                return {"new_evidence": [_evidence_for_prompt(e) for e in new], "deploys_returned": len(deploys)}
            if (tool, method) == ("memory", "search"):
                text = clean_text(str(args.get("text", "")), 300)
                results = tools.memory.search(text, service=service, k=3)
                new = ledger.add_similar(results, source="memory.search")
                return {"new_evidence": [_evidence_for_prompt(e) for e in new], "matches": len(results)}
        except ToolError as exc:
            ledger.warnings.append(f"{tool}.{method} failed: {clean_text(str(exc), 160)}")
            return {"error": clean_text(str(exc), 200)}
        return {"error": f"unsupported request {tool}.{method}"}

    def _merge_llm_answer(self, reply: dict[str, Any], ledger: EvidenceLedger, warnings: list[str]) -> list[Hypothesis]:
        items = reply.get("hypotheses")
        if not isinstance(items, list) or not 1 <= len(items) <= 5:
            raise ValueError("final answer needs 1 to 5 hypotheses")

        proposals: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("hypothesis is not an object")
            issue_type = str(item.get("issue_type", "")).strip()
            root_cause = clean_text(str(item.get("root_cause", "")), MAX_ROOT_CAUSE_CHARS)
            if issue_type not in ISSUE_TYPES:
                raise ValueError(f"unsupported issue_type '{issue_type}'")
            if not root_cause:
                raise ValueError("empty root_cause")
            for key in ("supporting", "contradicting"):
                cited = item.get(key, [])
                if not isinstance(cited, list):
                    raise ValueError(f"{key} must be a list of evidence ids")
                for evidence_id in cited:
                    if ledger.get(str(evidence_id)) is None:
                        raise ValueError(f"cites unknown evidence id {evidence_id}")
            if issue_type not in [p[0] for p in proposals]:
                proposals.append((issue_type, root_cause))

        candidates = score_all(ledger.items)  # recomputed: extra tool calls may have added evidence
        result: dict[str, Hypothesis] = {
            issue_type: self._hypothesis(0, candidates[issue_type], ledger, root_cause=text, source="llm")
            for issue_type, text in proposals
        }
        for issue_type, candidate in candidates.items():
            if issue_type not in result and candidate.confidence >= 0.3:
                result[issue_type] = self._hypothesis(0, candidate, ledger)

        ordered = sorted(result.values(), key=lambda h: (-h.confidence, h.issue_type))[:MAX_HYPOTHESES]
        strongest = max(candidates.values(), key=lambda c: (c.confidence, c.issue_type))
        if proposals[0][0] != ordered[0].issue_type:
            warnings.append(
                f"LLM ranked {proposals[0][0]} first but the evidence supports {ordered[0].issue_type} most strongly "
                f"({result[proposals[0][0]].confidence:.2f} vs {strongest.confidence:.2f}); ranked by evidence"
            )
        return [h.model_copy(update={"rank": rank}) for rank, h in enumerate(ordered, start=1)]


def _evidence_for_prompt(item: Evidence) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": item.id, "kind": item.kind, "summary": item.summary}
    samples = item.data.get("samples")
    if samples:
        entry["quotes"] = samples[:3]
    return entry
