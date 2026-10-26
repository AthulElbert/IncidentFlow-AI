from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.schemas import APMEvent

ISSUE_TYPES = (
    "application_error",
    "performance_degradation",
    "dependency_failure",
    "resource_saturation",
    "unknown",
)


class IncidentContext(BaseModel):
    incident_id: str
    service: str
    environment: str = "prod"
    alert_metric: str
    alert_message: str
    value: float
    threshold: float
    occurred_at: datetime

    @classmethod
    def from_event(cls, incident_id: str, event: APMEvent) -> "IncidentContext":
        return cls(
            incident_id=incident_id,
            service=event.service,
            environment=event.environment,
            alert_metric=event.metric,
            alert_message=event.message,
            value=event.value,
            threshold=event.threshold,
            occurred_at=event.timestamp,
        )


class Evidence(BaseModel):
    """One observation, with a stable id the hypotheses cite.

    `tags` are assigned by code from the data, never by a model. Confidence is computed
    from them, so a hypothesis can only be as strong as the signals actually observed.
    """

    id: str
    kind: str  # metric_anomaly | metric_normal | deploy | no_deploy | log_pattern | similar_incident | tool_error
    tags: list[str] = Field(default_factory=list)
    summary: str
    phrase: str = ""  # short clause used to word a root cause
    data: dict[str, Any] = Field(default_factory=dict)
    source: str = ""  # which tool call produced it


class Hypothesis(BaseModel):
    rank: int
    issue_type: str
    root_cause: str
    confidence: float
    supporting: list[str] = Field(default_factory=list)  # evidence ids
    contradicting: list[str] = Field(default_factory=list)
    missing_signals: list[str] = Field(default_factory=list)
    source: Literal["evidence", "llm"] = "evidence"


class InvestigationReport(BaseModel):
    incident_id: str
    service: str
    mode_used: Literal["heuristic", "llm"]
    decision: Literal["proceed", "escalate"]
    escalation_reason: str = ""
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tool_calls_used: int = 0

    @property
    def top(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None
