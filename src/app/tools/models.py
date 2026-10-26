from datetime import datetime

from pydantic import BaseModel, Field


class MetricPoint(BaseModel):
    timestamp: datetime
    value: float


class MetricSeries(BaseModel):
    service: str
    metric: str
    unit: str = ""
    points: list[MetricPoint] = Field(default_factory=list)

    def summary(self) -> dict[str, float]:
        """Compact view for prompts: the shape of the series without every point."""
        if not self.points:
            return {}
        values = [point.value for point in self.points]
        return {
            "count": float(len(values)),
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
            "first": values[0],
            "last": values[-1],
        }


class LogLine(BaseModel):
    timestamp: datetime
    service: str
    level: str = "INFO"
    message: str


class DeployRecord(BaseModel):
    service: str
    ref: str
    deployed_at: datetime
    deployed_by: str = ""
    kind: str = "code"  # code | config | rollback
    change_id: str | None = None
    status: str = "SUCCESS"
    summary: str = ""


class ScoredIncident(BaseModel):
    """A past incident retrieved for the current one, with why it matched."""

    incident_id: str
    score: float
    service: str
    metric: str
    issue_type: str
    summary: str
    resolution: str
    runbook_actions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class FileMatch(BaseModel):
    path: str
    line: int
    text: str


class EditResult(BaseModel):
    path: str
    lines: int
    changed: bool


class TestResult(BaseModel):
    __test__ = False  # not a pytest class

    passed: bool
    exit_code: int
    output: str
    duration_seconds: float


class DeployResult(BaseModel):
    status: str  # QUEUED | RUNNING | SUCCESS | FAILURE | ABORTED | UNKNOWN
    url: str = ""
    detail: str = ""
