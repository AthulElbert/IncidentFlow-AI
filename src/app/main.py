import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from app.adapters.jenkins_client import wait_for_build
from app.audit import AuditEntry, JsonlAuditLog, PostgresAuditLog
from app.config import load_settings, validate_settings
from app.events.inprocess import InProcessBus
from app.events.postgres import PostgresBus
from app.logging_config import setup_logging
from app.models.schemas import (
    APMEvent,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ChangeRecord,
    DevExecuteRequest,
    DevExecuteResponse,
    IncidentResponse,
    MetricsSummary,
    PRPrepareRequest,
    PRPrepareResponse,
    PromoteRequest,
    PromoteResponse,
)
from app.security import AuthManager, Principal
from app.services.build_tracker import BuildTracker, promotion_result_for_status
from app.services.change_control import ChangeControlStore, ConcurrentModificationError, PolicyConfig
from app.services.code_fix_agent import CodeFixAgent
from app.services.correlation import AlertCorrelator
from app.services.dev_fix_executor import DevFixExecutor
from app.services.integration_factory import (
    build_apm_alert_source,
    build_apm_client,
    build_jenkins_client,
    build_jira_client,
    build_llm_client,
    build_pr_client,
)
from app.services.metrics import build_metrics_summary
from app.services.pipeline import SupportAgentPipeline
from app.services.pr_preparer import PRPreparationService
from app.services.scheduler import AlertDedupStore, IncidentScheduler
from app.services.triage_agent import TriageAgent
from app.storage import atomic_write_text
from app.tools.factory import build_toolkit

app = FastAPI(title="Production Support Agent MVP", version="0.11.0")

base_dir = Path(__file__).resolve().parents[2]
settings = load_settings(str(base_dir))
validate_settings(settings)
setup_logging(settings.log_level)
logger = logging.getLogger("app.main")

data_dir = base_dir / settings.data_dir
kb_path = str(data_dir / "incident_history.json")
change_path = str(data_dir / "change_records.json")
if settings.storage_backend == "postgres":
    audit_log = PostgresAuditLog(settings.database_url)
    event_bus = PostgresBus(settings.database_url)
else:
    audit_log = JsonlAuditLog(data_dir / "audit_log.jsonl")
    event_bus = InProcessBus()
change_store = ChangeControlStore(
    change_path,
    policy=PolicyConfig(
        min_confidence_for_prod=settings.min_confidence_for_prod,
        require_zero_warnings_for_prod=settings.require_zero_warnings_for_prod,
        allowed_jenkins_states_for_prod=settings.allowed_jenkins_states_for_prod,
        # With auth disabled every request is the same synthetic actor, so a
        # two-person rule cannot be enforced.
        enforce_separation_of_duties=settings.enforce_separation_of_duties and settings.auth_enabled,
    ),
    storage_backend=settings.storage_backend,
    database_url=settings.database_url,
    audit=audit_log,
    bus=event_bus,
)

jira_client, jira_mode = build_jira_client(settings)
jenkins_client, jenkins_mode = build_jenkins_client(settings)
apm_client, apm_mode = build_apm_client(settings)
apm_alert_source, apm_alerts_mode = build_apm_alert_source(settings, str(base_dir))
llm_client, triage_mode = build_llm_client(settings)
pr_client, pr_mode = build_pr_client(settings)
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
    build_wait_seconds=settings.jenkins_build_wait_seconds,
    poll_interval_seconds=settings.jenkins_poll_interval_seconds,
)
build_tracker = BuildTracker(
    change_store=change_store,
    jenkins_client=jenkins_client,
    dev_executor=dev_executor,
)
code_fix_agent = CodeFixAgent(
    llm_client=llm_client,
    max_latency_ms=settings.code_fix_max_latency_ms,
    max_error_probability=settings.code_fix_max_error_probability,
)
pr_preparer = PRPreparationService(
    pr_client=pr_client,
    test_mode=settings.test_evidence_mode,
    test_command=settings.test_evidence_command,
    repo_root=str(base_dir),
    timeout_seconds=settings.test_evidence_timeout_seconds,
    base_branch=settings.pr_base_branch,
    local_branch_mode=settings.pr_local_branch_mode,
    patch_output_dir=settings.pr_patch_output_dir,
    code_change_mode=settings.code_change_mode,
    code_change_allowed_paths=settings.code_change_allowed_paths,
    code_change_max_lines=settings.code_change_max_lines,
    code_change_auto_commit=settings.code_change_auto_commit,
    code_change_auto_push=settings.code_change_auto_push,
    code_fix_mode=settings.code_fix_mode,
    code_fix_target_path=settings.code_fix_target_path,
    code_fix_agent=code_fix_agent,
)

pipeline = SupportAgentPipeline(
    kb_file=kb_path,
    jira_client=jira_client,
    jenkins_client=jenkins_client,
    jira_mode=jira_mode,
    jenkins_mode=jenkins_mode,
    change_store=change_store,
    triage_agent=triage_agent,
    storage_backend=settings.storage_backend,
    database_url=settings.database_url,
)

# The tool layer the agent workflow will use (Phases 3+). Built now so a bad tool
# configuration fails at startup rather than in the middle of an incident.
toolkit, sandbox_provider, tool_modes = build_toolkit(
    settings,
    change_store=change_store,
    incident_records=pipeline.kb.all_records,
    jenkins_client=jenkins_client,
    base_dir=base_dir,
)

scheduler_dedup_path = str(base_dir / settings.scheduler_dedup_file)
alert_dedup_store = AlertDedupStore(file_path=scheduler_dedup_path)
scheduler = IncidentScheduler(
    alert_source=apm_alert_source,
    pipeline=pipeline,
    dedup_store=alert_dedup_store,
    poll_interval_seconds=settings.apm_poll_interval_seconds,
    correlator=AlertCorrelator(
        change_store,
        window_minutes=settings.correlation_window_minutes,
        actor=settings.scheduler_actor,
    ),
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
        "apm_alerts_mode": apm_alerts_mode,
        "apm_poll_mode": settings.apm_poll_mode,
        "triage_mode": triage_mode,
        "pr_mode": pr_mode,
        "pr_local_branch_mode": settings.pr_local_branch_mode,
        "code_change_mode": settings.code_change_mode,
        "code_fix_mode": settings.code_fix_mode,
        "tool_modes": tool_modes,
        "log_level": settings.log_level,
        "storage_backend": settings.storage_backend,
        "min_confidence_for_prod": settings.min_confidence_for_prod,
        "auth_enabled": settings.auth_enabled,
        "auth_mode": settings.auth_mode,
    },
)

@asynccontextmanager
async def _lifespan(_: FastAPI):
    if settings.apm_poll_mode == "poll":
        scheduler.start()
    yield
    scheduler.stop()


app.router.lifespan_context = _lifespan


@app.exception_handler(ConcurrentModificationError)
def _concurrent_modification_handler(_, exc: ConcurrentModificationError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.get("/health")
def health(principal: Principal = Depends(viewer_auth)) -> dict[str, str]:
    return {
        "status": "ok",
        "jira_mode": jira_mode,
        "jenkins_mode": jenkins_mode,
        "apm_mode": apm_mode,
        "apm_alerts_mode": apm_alerts_mode,
        "apm_poll_mode": settings.apm_poll_mode,
        "triage_mode": triage_mode,
        "pr_mode": pr_mode,
        "pr_local_branch_mode": settings.pr_local_branch_mode,
        "code_change_mode": settings.code_change_mode,
        "scheduler_running": str(scheduler.status()["running"]).lower(),
        "log_level": settings.log_level,
        "storage_backend": settings.storage_backend,
        "min_confidence_for_prod": str(settings.min_confidence_for_prod),
        "auth_role": principal.role,
        "auth_source": principal.source,
    }


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(base_dir / "frontend" / "dashboard.html")


@app.get("/v1/scheduler/status")
def get_scheduler_status(principal: Principal = Depends(approver_auth)) -> dict:
    _ = principal
    return scheduler.status()


@app.post("/v1/scheduler/run-once")
def run_scheduler_once(principal: Principal = Depends(approver_auth)) -> dict:
    _ = principal
    return scheduler.run_once()


@app.post("/v1/apm/mock-alerts")
def enqueue_mock_alert(event: APMEvent, source_alert_id: str | None = None, principal: Principal = Depends(approver_auth)) -> dict:
    _ = principal
    queue_path = base_dir / settings.apm_alert_queue_file
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    if queue_path.exists():
        raw = json.loads(queue_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, list):
            raw = []
    else:
        raw = []

    alert_id = source_alert_id or f"alert-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    raw.append(
        {
            "source_alert_id": alert_id,
            "service": event.service,
            "metric": event.metric,
            "value": event.value,
            "threshold": event.threshold,
            "environment": event.environment,
            "timestamp": event.timestamp.isoformat(),
            "message": event.message,
        }
    )
    atomic_write_text(queue_path, json.dumps(raw, indent=2))
    return {"status": "queued", "source_alert_id": alert_id, "queue_file": str(queue_path)}


@app.post("/v1/incidents/process", response_model=IncidentResponse)
def process_incident(
    event: APMEvent,
    triage_mode: str | None = Query(default=None, pattern="^(heuristic|llm)$"),
    principal: Principal = Depends(viewer_auth),
) -> IncidentResponse:
    _ = principal
    return pipeline.process_event(event, triage_mode_override=triage_mode)


@app.post("/v1/incidents/mock", response_model=IncidentResponse)
def process_mock_incident(
    triage_mode: str | None = Query(default=None, pattern="^(heuristic|llm)$"),
    principal: Principal = Depends(viewer_auth),
) -> IncidentResponse:
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
    return pipeline.process_event(sample, triage_mode_override=triage_mode)


@app.get("/v1/changes", response_model=list[ChangeRecord])
def list_changes(status: str | None = Query(default=None), principal: Principal = Depends(viewer_auth)) -> list[ChangeRecord]:
    _ = principal
    return change_store.list_changes(status=status)


@app.get("/v1/metrics/summary", response_model=MetricsSummary)
def get_metrics_summary(principal: Principal = Depends(viewer_auth)) -> MetricsSummary:
    _ = principal
    changes = change_store.list_changes()
    return build_metrics_summary(
        changes=changes,
        min_confidence_for_prod=settings.min_confidence_for_prod,
    )


@app.get("/v1/audit", response_model=list[AuditEntry])
def list_audit(
    limit: int = Query(default=100, ge=1, le=1000),
    principal: Principal = Depends(viewer_auth),
) -> list[AuditEntry]:
    """Most recent audit entries across all changes, oldest first."""
    _ = principal
    return audit_log.list(limit=limit)


@app.get("/v1/changes/{change_id}/audit", response_model=list[AuditEntry])
def get_change_audit(
    change_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(viewer_auth),
) -> list[AuditEntry]:
    """Everything that happened to one change, oldest first."""
    _ = principal
    if not change_store.get_change(change_id):
        raise HTTPException(status_code=404, detail="Change not found")
    return audit_log.list(entity_id=change_id, limit=limit)


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
    try:
        # Validate before triggering anything: a Jenkins build must not start for a change
        # that is already decided, promoting, or has a dev build in flight.
        record = change_store.ensure_dev_executable(change_id)
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


@app.post("/v1/changes/{change_id}/refresh-status", response_model=ChangeRecord)
def refresh_build_status(change_id: str, principal: Principal = Depends(approver_auth)) -> ChangeRecord:
    """Poll Jenkins for a dev build or prod deploy that was still running when last checked."""
    try:
        return build_tracker.refresh(change_id, actor=principal.actor)
    except ValueError as exc:
        msg = str(exc)
        if msg == "Change not found":
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except ConcurrentModificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Status refresh failed: {exc}") from exc


@app.post("/v1/changes/{change_id}/prepare-pr", response_model=PRPrepareResponse)
def prepare_pr(
    change_id: str,
    payload: PRPrepareRequest,
    principal: Principal = Depends(approver_auth),
) -> PRPrepareResponse:
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")

    try:
        prepared = pr_preparer.prepare(record, requested_by=principal.actor, comment=payload.comment)
        updated = change_store.record_pr_preparation(
            change_id=change_id,
            generated_by=principal.actor,
            pr_status=prepared.pr.status,
            pr_url=prepared.pr.url,
            pr_branch=prepared.pr.branch,
            pr_title=prepared.pr.title,
            pr_summary=prepared.pr_summary,
            patch_artifact_path=prepared.patch_artifact_path,
            patch_preview=prepared.patch_preview,
            local_branch_created=prepared.local_branch_created,
            local_branch_message=prepared.local_branch_message,
            code_change_status=prepared.code_change_status,
            code_change_message=prepared.code_change_message,
            sandbox_worktree_path=prepared.sandbox_worktree_path,
            changed_files=prepared.changed_files,
            commit_sha=prepared.commit_sha,
            push_status=prepared.push_status,
            test_evidence_status=prepared.test_evidence_status,
            test_command=prepared.test_command,
            test_output=prepared.test_output,
            test_pass_rate=prepared.test_pass_rate,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "Change not found":
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PR preparation failed: {exc}") from exc

    return PRPrepareResponse(
        change_id=updated.change_id,
        pr_status=updated.pr_status,
        pr_url=updated.pr_url or "",
        pr_branch=updated.pr_branch or "",
        local_branch_created=updated.local_branch_created,
        local_branch_message=updated.local_branch_message or "",
        patch_artifact_path=updated.patch_artifact_path or "",
        code_change_status=updated.code_change_status,
        code_change_message=updated.code_change_message or "",
        sandbox_worktree_path=updated.sandbox_worktree_path or "",
        changed_files=updated.changed_files,
        commit_sha=updated.commit_sha or "",
        push_status=updated.push_status or "",
        test_evidence_status=updated.test_evidence_status,
        test_command=updated.test_command or "",
        test_pass_rate=updated.test_pass_rate or 0.0,
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
    # 1. Validate and reserve the promotion. Nothing external has happened yet, so an
    #    unapproved, rejected, blocked, already-promoting or same-actor request ends here
    #    without a deploy ever being triggered.
    try:
        reserved = change_store.begin_promotion(change_id, payload, promoted_by=principal.actor)
    except ValueError as exc:
        message = str(exc)
        if message == "Change not found":
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc
    except ConcurrentModificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 2. Trigger the deploy. A failure here is recorded, never left stuck in `promoting`.
    try:
        prod_build = jenkins_client.trigger_prod_deploy(service=reserved.service, change_id=change_id)
    except Exception as exc:
        change_store.finalize_promotion(
            change_id, "failed", "", note=f"deploy trigger failed: {exc}", actor=principal.actor
        )
        raise HTTPException(status_code=502, detail=f"Failed to trigger Jenkins prod deploy: {exc}") from exc

    # 3. Give the build a bounded chance to finish. If polling itself fails or the build is
    #    still running, the change stays `promoting` until refresh-status confirms it.
    try:
        prod_build = wait_for_build(
            jenkins_client,
            prod_build,
            settings.jenkins_build_wait_seconds,
            settings.jenkins_poll_interval_seconds,
        )
    except Exception as exc:
        logger.warning("prod-build-poll-failed", extra={"change_id": change_id, "error": str(exc)})

    # 4. Only a confirmed SUCCESS becomes promoted_to_prod.
    updated = change_store.finalize_promotion(
        change_id,
        promotion_result_for_status(prod_build.status),
        prod_build.url,
        actor=principal.actor,
    )

    return PromoteResponse(
        change_id=updated.change_id,
        deployment_state=updated.deployment_state,
        promotion_result=updated.promotion_result or "queued",
        promoted_by=updated.promoted_by or principal.actor,
        promoted_at=updated.promoted_at,
        prod_deploy_url=updated.prod_deploy_url or prod_build.url,
    )
