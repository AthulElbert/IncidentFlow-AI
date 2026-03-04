from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class APMEvent(BaseModel):
    service: str
    metric: str
    value: float
    threshold: float
    environment: str = "prod"
    timestamp: datetime
    message: str


class IncidentRecord(BaseModel):
    incident_id: str
    service: str
    metric: str
    issue_type: str
    summary: str
    resolution: str
    runbook_actions: list[str] = Field(default_factory=list)
    created_at: datetime


class JiraTicket(BaseModel):
    key: str
    summary: str
    description: str
    labels: list[str] = Field(default_factory=list)


class JenkinsBuildResult(BaseModel):
    job_name: str
    build_number: int
    status: str
    url: str


class SimilarIncident(BaseModel):
    incident_id: str
    score: float
    summary: str
    resolution: str


class ChangeRecord(BaseModel):
    change_id: str
    incident_id: str
    service: str
    environment: str
    summary: str
    jira_key: str
    jenkins_job_url: str
    proposed_actions: list[str] = Field(default_factory=list)
    issue_type: str = "unknown"
    confidence: float = 0.0
    warning_count: int = 0
    jenkins_status: str = "UNKNOWN"
    dev_execution_status: Literal["not_started", "in_progress", "passed", "failed"] = "not_started"
    dev_execution_url: str | None = None
    dev_apm_improvement_pct: float | None = None
    dev_smoke_tests_passed: bool | None = None
    dev_notes: str | None = None
    dev_executed_at: datetime | None = None
    dev_executed_by: str | None = None
    status: Literal["pending_approval", "approved", "rejected"] = "pending_approval"
    deployment_state: Literal[
        "awaiting_dev_execution",
        "dev_fix_in_progress",
        "ready_for_prod",
        "blocked_by_policy",
        "dev_fix_failed",
        "closed_rejected",
        "promoted_to_prod",
        "prod_promotion_failed",
    ] = "awaiting_dev_execution"
    policy_reasons: list[str] = Field(default_factory=list)
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_comment: str | None = None
    promoted_at: datetime | None = None
    promoted_by: str | None = None
    promotion_comment: str | None = None
    promotion_result: Literal["queued", "success", "failed"] | None = None
    prod_deploy_url: str | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = ""


class ApprovalDecisionResponse(BaseModel):
    change_id: str
    status: str
    deployment_state: str
    policy_reasons: list[str] = Field(default_factory=list)
    decided_by: str
    decided_at: datetime


class DevExecuteRequest(BaseModel):
    comment: str = ""


class DevExecuteResponse(BaseModel):
    change_id: str
    deployment_state: str
    dev_execution_status: str
    dev_execution_url: str
    validation_passed: bool


class PromoteRequest(BaseModel):
    comment: str = ""


class PromoteResponse(BaseModel):
    change_id: str
    deployment_state: str
    promotion_result: str
    promoted_by: str
    promoted_at: datetime
    prod_deploy_url: str


class IncidentResponse(BaseModel):
    issue_type: str
    confidence: float
    is_recurring: bool
    recurrence_count: int
    probable_cause: str
    suggested_actions: list[str]
    similar_incidents: list[SimilarIncident] = Field(default_factory=list)
    jira_ticket: JiraTicket
    jenkins_validation: JenkinsBuildResult
    metadata: dict[str, Any] = Field(default_factory=dict)
