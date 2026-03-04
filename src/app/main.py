import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query

from app.config import load_settings
from app.logging_config import setup_logging
from app.models.schemas import (
    APMEvent,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ChangeRecord,
    DevExecuteRequest,
    DevExecuteResponse,
    IncidentResponse,
    PromoteRequest,
    PromoteResponse,
)
from app.security import AuthManager, Principal
from app.services.change_control import ChangeControlStore, PolicyConfig
from app.services.dev_fix_executor import DevFixExecutor
from app.services.integration_factory import (
    build_apm_client,
    build_jenkins_client,
    build_jira_client,
    build_llm_client,
)
from app.services.pipeline import SupportAgentPipeline
from app.services.triage_agent import TriageAgent

app = FastAPI(title="Production Support Agent MVP", version="0.10.0")

base_dir = Path(__file__).resolve().parents[2]
settings = load_settings(str(base_dir))
setup_logging(settings.log_level)
logger = logging.getLogger("app.main")

kb_path = str(base_dir / "data" / "incident_history.json")
change_path = str(base_dir / "data" / "change_records.json")
change_store = ChangeControlStore(
    change_path,
    policy=PolicyConfig(
        min_confidence_for_prod=settings.min_confidence_for_prod,
        require_zero_warnings_for_prod=settings.require_zero_warnings_for_prod,
        allowed_jenkins_states_for_prod=settings.allowed_jenkins_states_for_prod,
    ),
)

jira_client, jira_mode = build_jira_client(settings)
jenkins_client, jenkins_mode = build_jenkins_client(settings)
apm_client, apm_mode = build_apm_client(settings)
llm_client, triage_mode = build_llm_client(settings)
triage_agent = TriageAgent(
    mode=settings.triage_mode,
    llm_client=llm_client,
    confidence_floor=settings.triage_confidence_floor,
)
dev_executor = DevFixExecutor(
    jenkins_client=jenkins_client,
    apm_client=apm_client,
    min_apm_improvement_pct=settings.dev_min_apm_improvement_pct,
    require_smoke_tests=settings.require_dev_smoke_tests,
)

pipeline = SupportAgentPipeline(
    kb_file=kb_path,
    jira_client=jira_client,
    jenkins_client=jenkins_client,
    jira_mode=jira_mode,
    jenkins_mode=jenkins_mode,
    change_store=change_store,
    triage_agent=triage_agent,
)

auth = AuthManager(
    enabled=settings.auth_enabled,
    mode=settings.auth_mode,
    key_registry={
        settings.viewer_api_key: Principal(actor=settings.viewer_actor, role="viewer", source="api_key"),
        settings.approver_api_key: Principal(actor=settings.approver_actor, role="approver", source="api_key"),
        settings.release_operator_api_key: Principal(actor=settings.release_operator_actor, role="release_operator", source="api_key"),
    },
    jwt_secret=settings.jwt_secret,
    jwt_algorithm=settings.jwt_algorithm,
    jwt_issuer=settings.jwt_issuer,
    jwt_audience=settings.jwt_audience,
    jwt_role_claim=settings.jwt_role_claim,
    jwt_actor_claim=settings.jwt_actor_claim,
)

viewer_auth = auth.authorize({"viewer", "approver", "release_operator"})
approver_auth = auth.authorize({"approver", "release_operator"})
release_auth = auth.authorize({"release_operator"})

logger.info(
    "support-agent-started",
    extra={
        "jira_mode": jira_mode,
        "jenkins_mode": jenkins_mode,
        "apm_mode": apm_mode,
        "triage_mode": triage_mode,
        "log_level": settings.log_level,
        "min_confidence_for_prod": settings.min_confidence_for_prod,
        "auth_enabled": settings.auth_enabled,
        "auth_mode": settings.auth_mode,
    },
)


@app.get("/health")
def health(principal: Principal = Depends(viewer_auth)) -> dict[str, str]:
    return {
        "status": "ok",
        "jira_mode": jira_mode,
        "jenkins_mode": jenkins_mode,
        "apm_mode": apm_mode,
        "triage_mode": triage_mode,
        "log_level": settings.log_level,
        "min_confidence_for_prod": str(settings.min_confidence_for_prod),
        "auth_role": principal.role,
        "auth_source": principal.source,
    }


@app.post("/v1/incidents/process", response_model=IncidentResponse)
def process_incident(event: APMEvent, principal: Principal = Depends(viewer_auth)) -> IncidentResponse:
    _ = principal
    return pipeline.process_event(event)


@app.post("/v1/incidents/mock", response_model=IncidentResponse)
def process_mock_incident(principal: Principal = Depends(viewer_auth)) -> IncidentResponse:
    _ = principal
    sample = APMEvent(
        service="payments-service",
        metric="latency_p99_ms",
        value=1250,
        threshold=800,
        environment="prod",
        timestamp=datetime.now(timezone.utc),
        message="High latency observed in checkout endpoint",
    )
    return pipeline.process_event(sample)


@app.get("/v1/changes", response_model=list[ChangeRecord])
def list_changes(status: str | None = Query(default=None), principal: Principal = Depends(viewer_auth)) -> list[ChangeRecord]:
    _ = principal
    return change_store.list_changes(status=status)


@app.get("/v1/changes/{change_id}", response_model=ChangeRecord)
def get_change(change_id: str, principal: Principal = Depends(viewer_auth)) -> ChangeRecord:
    _ = principal
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")
    return record


@app.post("/v1/changes/{change_id}/execute-dev", response_model=DevExecuteResponse)
def execute_dev_fix(
    change_id: str,
    payload: DevExecuteRequest,
    principal: Principal = Depends(approver_auth),
) -> DevExecuteResponse:
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")

    try:
        exec_url, jenkins_status, apm_improvement, smoke_ok, validation_passed, notes = dev_executor.execute(record)
        updated = change_store.record_dev_execution(
            change_id=change_id,
            executed_by=principal.actor,
            execution_url=exec_url,
            jenkins_status=jenkins_status,
            apm_improvement_pct=apm_improvement,
            smoke_tests_passed=smoke_ok,
            validation_passed=validation_passed,
            notes=(payload.comment + " | " if payload.comment else "") + notes,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "Change not found":
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dev execution failed: {exc}") from exc

    return DevExecuteResponse(
        change_id=updated.change_id,
        deployment_state=updated.deployment_state,
        dev_execution_status=updated.dev_execution_status,
        dev_execution_url=updated.dev_execution_url or "",
        validation_passed=updated.dev_execution_status == "passed",
    )


@app.post("/v1/changes/{change_id}/decision", response_model=ApprovalDecisionResponse)
def decide_change(
    change_id: str,
    payload: ApprovalDecisionRequest,
    principal: Principal = Depends(approver_auth),
) -> ApprovalDecisionResponse:
    try:
        updated = change_store.apply_decision(change_id, payload, decided_by=principal.actor)
    except ValueError as exc:
        message = str(exc)
        if message == "Change not found":
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc

    return ApprovalDecisionResponse(
        change_id=updated.change_id,
        status=updated.status,
        deployment_state=updated.deployment_state,
        policy_reasons=updated.policy_reasons,
        decided_by=updated.decided_by or principal.actor,
        decided_at=updated.decided_at or datetime.now(timezone.utc),
    )


@app.post("/v1/changes/{change_id}/promote", response_model=PromoteResponse)
def promote_change(
    change_id: str,
    payload: PromoteRequest,
    principal: Principal = Depends(release_auth),
) -> PromoteResponse:
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")

    try:
        prod_build = jenkins_client.trigger_prod_deploy(service=record.service, change_id=change_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to trigger Jenkins prod deploy: {exc}") from exc

    status = (prod_build.status or "UNKNOWN").upper()
    if status in {"SUCCESS"}:
        promotion_result = "success"
    elif status in {"QUEUED", "RUNNING", "IN_PROGRESS"}:
        promotion_result = "queued"
    else:
        promotion_result = "failed"

    try:
        updated = change_store.promote_change(
            change_id=change_id,
            payload=payload,
            promoted_by=principal.actor,
            promotion_result=promotion_result,
            prod_deploy_url=prod_build.url,
        )
    except ValueError as exc:
        message = str(exc)
        if message == "Change not found":
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc

    return PromoteResponse(
        change_id=updated.change_id,
        deployment_state=updated.deployment_state,
        promotion_result=updated.promotion_result or promotion_result,
        promoted_by=updated.promoted_by or principal.actor,
        promoted_at=updated.promoted_at or datetime.now(timezone.utc),
        prod_deploy_url=updated.prod_deploy_url or prod_build.url,
    )
