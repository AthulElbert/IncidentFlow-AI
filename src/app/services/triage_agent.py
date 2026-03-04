from dataclasses import dataclass

from app.adapters.llm_client import LLMClient
from app.models.schemas import APMEvent
from app.services.classifier import classify_issue

_ALLOWED_ISSUES = {
    "performance_degradation",
    "application_error",
    "resource_saturation",
    "dependency_failure",
    "unknown",
}


@dataclass(frozen=True)
class TriageResult:
    issue_type: str
    confidence: float
    probable_cause: str
    hypothesis_steps: list[str]
    mode_used: str
    warnings: list[str]


class TriageAgent:
    def __init__(
        self,
        mode: str = "heuristic",
        llm_client: LLMClient | None = None,
        confidence_floor: float = 0.50,
    ) -> None:
        self.mode = mode
        self.llm_client = llm_client
        self.confidence_floor = confidence_floor

    def _heuristic(self, event: APMEvent, warning: str | None = None) -> TriageResult:
        issue_type, confidence, probable_cause = classify_issue(event)
        hints = [
            f"Metric={event.metric}, value={event.value}, threshold={event.threshold}",
            f"Message signal: {event.message}",
            f"Environment={event.environment}, service={event.service}",
        ]
        warnings: list[str] = []
        if warning:
            warnings.append(warning)
        return TriageResult(
            issue_type=issue_type,
            confidence=confidence,
            probable_cause=probable_cause,
            hypothesis_steps=hints,
            mode_used="heuristic",
            warnings=warnings,
        )

    def triage(self, event: APMEvent) -> TriageResult:
        if self.mode != "llm" or self.llm_client is None:
            return self._heuristic(event)

        try:
            raw = self.llm_client.triage(event)
            issue_type = str(raw.get("issue_type", "unknown")).strip()
            confidence = float(raw.get("confidence", 0.0))
            probable_cause = str(raw.get("probable_cause", "")).strip()
            steps = raw.get("hypothesis_steps", [])
            if not isinstance(steps, list):
                steps = []
            hypothesis_steps = [str(x).strip() for x in steps if str(x).strip()][:6]

            if issue_type not in _ALLOWED_ISSUES:
                raise ValueError(f"unsupported issue_type={issue_type}")
            if confidence < 0 or confidence > 1:
                raise ValueError("confidence outside 0..1")
            if not probable_cause:
                raise ValueError("empty probable_cause")
            if confidence < self.confidence_floor:
                raise ValueError(
                    f"confidence {confidence:.2f} below floor {self.confidence_floor:.2f}"
                )

            if not hypothesis_steps:
                hypothesis_steps = ["LLM triage returned no explicit hypothesis steps."]

            return TriageResult(
                issue_type=issue_type,
                confidence=confidence,
                probable_cause=probable_cause,
                hypothesis_steps=hypothesis_steps,
                mode_used="llm",
                warnings=[],
            )
        except Exception as exc:
            return self._heuristic(event, warning=f"LLM triage fallback: {exc}")
