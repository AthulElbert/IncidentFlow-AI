import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse

from app.adapters.jenkins_client import wait_for_build
from app.audit import AuditEntry, JsonlAuditLog, PostgresAuditLog
from app.config import load_settings, validate_settings
from app.events.bus import INCIDENT_EVENTS_TOPIC
from app.events.inprocess import InProcessBus
from app.events.kafka import KafkaBus, relay
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
    PostmortemFinalizeRequest,
    PromoteRequest,
    PromoteResponse,
)
from app.audit import new_audit_entry
from app.config import ownership_file_path
from app.policy import OpaPolicy, PythonPolicy, authz_input, load_ownership
from app.security import AuthManager, OidcVerifier, Principal
from app.services.build_tracker import BuildTracker, promotion_result_for_status
from app.services.change_control import ChangeControlStore, ConcurrentModificationError, PolicyConfig
from app.services.code_fix_agent import CodeFixAgent
from app.services.correlation import AlertCorrelator
from app.services.dev_fix_executor import DevFixExecutor
from app.services.consumer import ConsumerLoop
from app.services.integration_factory import (
    build_apm_alert_source,
    build_apm_client,
    build_critic_llm,
    build_fixer_llm,
    build_investigator_llm,
    build_jenkins_client,
    build_jira_client,
    build_llm_client,
    llm_for_profile,
    build_pr_client,
    raise_unless_mock_fallback,
)
from app.services.learning import OutcomeLearner
from app.services.metrics import build_metrics_summary
from app.services.pipeline import SupportAgentPipeline
from app.services.pr_preparer import PRPreparationService
from app.services.postmortem import build_postmortem
from app.services.shadow import build_shadow_report
from app.services.review import build_review, overview_row
from app.services.scheduler import AlertDedupStore, IncidentScheduler
from app.services.triage_agent import TriageAgent
from app.storage import atomic_write_text
from app.replay import event_for
from app.tools.factory import build_memory, build_toolkit
from app.tools.memory import LexicalIncidentMemory
from app.services.memory_indexer import MemorySync
from app.tools.scenarios import DEFAULT_SCENARIOS
from app.agents.budget import BudgetLimits
from app.agents.checkpoint import build_checkpointing
from app.agents.critic import Critic
from app.agents.graph import AgentRuntime
from app.agents.fixer import Fixer
from app.agents.investigator import Investigator
from app.agents.runner import AgentRunner
from app.agents.verifier import Verifier

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
    event_bus = PostgresBus(settings.database_url)  # the transactional outbox: written with each state change
else:
    audit_log = JsonlAuditLog(data_dir / "audit_log.jsonl")
    event_bus = InProcessBus()
# Kafka is optional. With Postgres storage the outbox above stays the store's bus and `relay` forwards it to Kafka;
# with JSON storage the store publishes to Kafka directly after each write (best effort, like the in-process bus).
kafka_bus = None
consumer_bus = event_bus
if settings.event_bus_mode == "kafka":
    kafka_bus = KafkaBus(settings.kafka_bootstrap_servers, client_id=settings.kafka_client_id)
    consumer_bus = kafka_bus
    if settings.storage_backend != "postgres":
        event_bus = kafka_bus
# Policy: the production gate and service-scoped authorisation, decided by the same engine. OPA fails closed: if it
# cannot be reached at startup the app does not start, and a decision it cannot make is a refusal.
policy_engine = OpaPolicy(settings.opa_url, settings.opa_timeout_seconds) if settings.policy_engine == "opa" else PythonPolicy()
ownership = load_ownership(ownership_file_path(settings)) if settings.authz_mode == "scoped" else None

change_store = ChangeControlStore(
    change_path,
    policy_engine=policy_engine,
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
    llm_client=llm_for_profile(llm_client, "triage"),
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
    llm_client=llm_for_profile(llm_client, "code_fix"),
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

# Incident memory. Hybrid memory (pgvector) is fed by an indexer: at startup for anything not yet indexed, when an
# outcome is learned, and when a person finalises a postmortem.
try:
    incident_memory, memory_indexer, memory_mode = build_memory(settings, pipeline.kb.all_records)
except Exception as exc:
    raise_unless_mock_fallback(settings, "Incident memory (pgvector)", exc)
    incident_memory, memory_indexer, memory_mode = LexicalIncidentMemory(pipeline.kb.all_records), None, "lexical-fallback-startup"
memory_sync = MemorySync(memory_indexer, pipeline.kb, change_store) if memory_indexer is not None else None
if memory_sync is not None:
    memory_sync.sync_all()

# The tool layer the agent workflow will use (Phases 3+). Built now so a bad tool
# configuration fails at startup rather than in the middle of an incident.
toolkit, sandbox_provider, tool_modes = build_toolkit(
    settings,
    change_store=change_store,
    incident_records=pipeline.kb.all_records,
    jenkins_client=jenkins_client,
    base_dir=base_dir,
    memory=incident_memory,
    memory_mode=memory_mode,
)

investigator = Investigator(
    llm=build_investigator_llm(settings, llm_client),
    min_confidence=settings.investigation_min_confidence,
    min_margin=settings.investigation_min_margin,
    max_llm_steps=settings.investigation_max_llm_steps,
    max_follow_ups=settings.investigation_follow_ups,
)

verifier = Verifier(
    allowed_paths=[part.strip() for part in settings.code_change_allowed_paths.split(",") if part.strip()],
    max_changed_lines=settings.code_change_max_lines,
    deploy_wait_seconds=settings.verifier_deploy_wait_seconds,
    poll_interval_seconds=settings.verifier_poll_interval_seconds,
    soak_minutes=settings.verifier_soak_minutes,
    min_improvement=settings.verifier_min_improvement,
)

_editable_paths = [part.strip() for part in settings.code_change_allowed_paths.split(",") if part.strip()]
fixer = Fixer(
    llm=build_fixer_llm(settings, llm_client),
    max_steps=settings.fixer_max_steps,
    editable_paths=_editable_paths,
    max_changed_lines=settings.code_change_max_lines,
)
critic = Critic(
    llm=build_critic_llm(settings, llm_client),
    min_improvement=settings.verifier_min_improvement,
)

agent_runtime = AgentRuntime(
    change_store=change_store,
    toolkit=toolkit,
    sandbox_provider=sandbox_provider,
    investigator=investigator,
    fixer=fixer,
    verifier=verifier,
    critic=critic,
    audit=audit_log,
    max_attempts=settings.agent_max_fix_attempts,
    budget_limits=BudgetLimits(
        max_seconds=settings.agent_run_max_seconds,
        max_tool_calls=settings.agent_run_max_tool_calls,
        max_llm_calls=settings.agent_run_max_llm_calls,
        max_tokens=settings.agent_run_max_tokens,
        max_cost=settings.agent_run_max_cost,
    ),
)
checkpointing = build_checkpointing(
    settings.checkpoint_backend,
    sqlite_file=settings.checkpoint_sqlite_file or (data_dir / "agent_checkpoints.sqlite"),
    database_url=settings.database_url,
)
agent_runner = AgentRunner(
    agent_runtime,
    workers=settings.agent_run_workers,
    checkpointer=checkpointing.saver,
    max_resumes=settings.agent_run_max_resumes,
)
# A restart interrupts any run that was in flight. With a durable checkpointer it is resumed from its last
# completed stage; otherwise (and for shadow runs) it is escalated, so nothing is left looking "running".
run_recovery = agent_runner.recover()
if run_recovery["resumed"] or run_recovery["escalated"]:
    logger.info("agent-runs-recovered", extra=run_recovery)

# What happened to a change becomes what incident memory knows. The sweep covers events the bus lost in a restart.
outcome_learner = OutcomeLearner(
    change_store, pipeline.kb, consumer_bus, on_learned=memory_sync.incident if memory_sync is not None else None
)
outcome_learner.reconcile()
learning_loop = ConsumerLoop("learning", outcome_learner.run_once, interval_seconds=2.0)
relay_loop = (
    ConsumerLoop("kafka-relay", lambda: relay(event_bus, kafka_bus, INCIDENT_EVENTS_TOPIC), interval_seconds=1.0)
    if kafka_bus is not None and settings.storage_backend == "postgres"
    else None
)
if settings.agent_run_mode == "auto":
    pipeline.on_change_created = lambda change_id: agent_runner.start(change_id, actor="agent:orchestrator")
elif settings.agent_run_mode == "shadow":
    # Observe only: the agents work on every new incident, but nothing they produce enters the approval flow.
    pipeline.on_change_created = lambda change_id: agent_runner.start_shadow(change_id, actor="agent:orchestrator")

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
        settings.viewer_api_key: Principal(actor=settings.viewer_actor, role="viewer", source="api_key", teams=frozenset(t.strip() for t in settings.viewer_teams.split(",") if t.strip())),
        settings.approver_api_key: Principal(actor=settings.approver_actor, role="approver", source="api_key", teams=frozenset(t.strip() for t in settings.approver_teams.split(",") if t.strip())),
        settings.release_operator_api_key: Principal(
            actor=settings.release_operator_actor, role="release_operator", source="api_key", teams=frozenset(t.strip() for t in settings.release_operator_teams.split(",") if t.strip())
        ),
    },
    jwt_secret=settings.jwt_secret,
    jwt_algorithm=settings.jwt_algorithm,
    jwt_issuer=settings.jwt_issuer,
    jwt_audience=settings.jwt_audience,
    jwt_role_claim=settings.jwt_role_claim,
    jwt_actor_claim=settings.jwt_actor_claim,
    jwt_teams_claim=settings.jwt_teams_claim,
    oidc=OidcVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
        algorithms=tuple(a.strip() for a in settings.oidc_algorithms.split(",") if a.strip()),
        actor_claim=settings.oidc_actor_claim,
        roles_claim=settings.oidc_roles_claim,
        teams_claim=settings.oidc_teams_claim,
    )
    if settings.auth_mode == "oidc"
    else None,
)

APPROVER_ROLES = {"approver", "release_operator"}
viewer_auth = auth.authorize({"viewer", "approver", "release_operator"})
approver_auth = auth.authorize(APPROVER_ROLES)
release_auth = auth.authorize({"release_operator"})


def scope_decision(principal: Principal, action: str, service: str):
    """Service-scoped authorisation (AUTHZ_MODE=scoped): None when not scoped, else the policy decision."""
    if ownership is None or principal.source == "disabled":
        return None
    return policy_engine.authorize(authz_input(principal, action, service, ownership))


def change_auth(action: str, roles: set[str]):
    """The role check, then (when scoped) whether this person may act on this change's service. Both are audited."""
    role_check = auth.authorize(roles)

    def dependency(change_id: str, principal: Principal = Depends(role_check)) -> Principal:
        record = change_store.get_change(change_id)
        if record is None:
            return principal  # the endpoint answers 404
        decision = scope_decision(principal, action, record.service)
        if decision is None:
            return principal
        audit_log.append(
            new_audit_entry(
                actor=principal.actor,
                action="authz.allowed" if decision.allow else "authz.denied",
                entity_id=change_id,
                from_state=record.deployment_state,
                to_state=record.deployment_state,
                details={"action": action, "service": record.service, "reason": "; ".join(decision.reasons), "policy": decision.log()},
            )
        )
        if not decision.allow:
            status_code = 503 if decision.error else 403
            raise HTTPException(status_code=status_code, detail="; ".join(decision.reasons))
        return principal

    return dependency

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
        "investigator_mode": settings.investigator_mode,
        "agent_run_mode": settings.agent_run_mode,
        "fixer_mode": settings.fixer_mode,
        "critic_mode": settings.critic_mode,
        "dev_environment": tool_modes.get("deploy_target"),
        "log_level": settings.log_level,
        "storage_backend": settings.storage_backend,
        "min_confidence_for_prod": settings.min_confidence_for_prod,
        "auth_enabled": settings.auth_enabled,
        "auth_mode": settings.auth_mode,
        "authz_mode": settings.authz_mode,
        "policy_engine": policy_engine.name,
        "policy_bundle": policy_engine.bundle,
    },
)

@asynccontextmanager
async def _lifespan(_: FastAPI):
    if settings.apm_poll_mode == "poll":
        scheduler.start()
    learning_loop.start()
    if relay_loop is not None:
        relay_loop.start()
    yield
    if relay_loop is not None:
        relay_loop.stop()
    learning_loop.stop()
    scheduler.stop()
    agent_runner.shutdown()
    checkpointing.close()
    if toolkit.mcp is not None:
        toolkit.mcp.close()


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


@app.get("/v1/llm/status")
def llm_status(principal: Principal = Depends(viewer_auth)) -> dict:
    """Which model each task uses and whether each endpoint is healthy. Contains no keys."""
    _ = principal
    router = getattr(llm_client, "router", None)
    return {"configured": router is not None, **(router.status() if router is not None else {})}


@app.get("/v1/whoami")
def whoami(principal: Principal = Depends(viewer_auth)) -> dict:
    """Who the credentials in this request belong to, so a screen can tell whether two actions are by the same person."""
    return {"actor": principal.actor, "role": principal.role, "roles": sorted(principal.all_roles), "teams": sorted(principal.teams)}


# The review's actions and the role each needs (see services/review.build_actions).
_REVIEW_ACTION_ROLES = {
    "run_agents": APPROVER_ROLES,
    "approve": APPROVER_ROLES,
    "reject": APPROVER_ROLES,
    "promote": {"release_operator"},
    "refresh_status": APPROVER_ROLES,
}
_REVIEW_ACTION_POLICY_NAMES = {"approve": "decide", "reject": "decide"}


@app.get("/v1/changes/{change_id}/access")
def get_change_access(change_id: str, principal: Principal = Depends(viewer_auth)) -> dict:
    """What the caller may do on this change, as far as roles and service ownership go ("" means allowed).

    The review says whether an action is open at all; this says whether it is open to *you*. Not audited: it decides
    nothing, and the action itself is checked again when it is taken.
    """
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")
    actions: dict[str, str] = {}
    for name, roles in _REVIEW_ACTION_ROLES.items():
        if principal.source != "disabled" and not (principal.all_roles & roles):
            actions[name] = f"needs the {' or '.join(sorted(roles))} role"
            continue
        decision = scope_decision(principal, _REVIEW_ACTION_POLICY_NAMES.get(name, name), record.service)
        actions[name] = "" if decision is None or decision.allow else "; ".join(decision.reasons)
    return {"actor": principal.actor, "teams": sorted(principal.teams), "scoped": ownership is not None, "actions": actions}


@app.get("/v1/changes/overview")
def changes_overview(
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(viewer_auth),
) -> list[dict]:
    """A small row per change, newest first. Cheap enough to poll; `version` changes when anything about the change does."""
    _ = principal
    changes = sorted(change_store.list_changes(), key=lambda item: item.created_at, reverse=True)
    return [overview_row(item) for item in changes[:limit]]


@app.get("/v1/changes/{change_id}/review")
def get_change_review(change_id: str, principal: Principal = Depends(viewer_auth)) -> dict:
    """Everything a person needs to decide about a change: the diagnosis, the fix and its checks, open actions, a timeline."""
    _ = principal
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")
    entries = audit_log.list(entity_id=change_id, limit=500)
    return jsonable_encoder(build_review(record, entries, change_store, settings.agent_run_mode))


@app.get("/v1/changes/{change_id}/postmortem")
def get_change_postmortem(change_id: str, principal: Principal = Depends(viewer_auth)) -> dict:
    """A postmortem draft built from the platform's records. Always a draft; a person edits and owns it."""
    _ = principal
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")
    entries = audit_log.list(entity_id=change_id, limit=500)
    review = jsonable_encoder(build_review(record, entries, change_store, settings.agent_run_mode))
    pm = record.postmortem or {}
    if pm.get("markdown"):
        # The version a person finalised is the postmortem; the draft is only where it started.
        return {"change_id": change_id, "final": True, "finalized": True, "markdown": pm["markdown"],
                "finalized_by": pm.get("finalized_by"), "finalized_at": pm.get("finalized_at"), "revision": pm.get("revision")}
    return {"change_id": change_id, "final": review["terminal"], "finalized": False, "markdown": build_postmortem(record, review)}


@app.put("/v1/changes/{change_id}/postmortem")
def finalize_change_postmortem(
    change_id: str, payload: PostmortemFinalizeRequest, principal: Principal = Depends(change_auth("finalize_postmortem", APPROVER_ROLES))
) -> dict:
    """A person signs off the postmortem. Its sections become searchable incident memory if the incident was resolved."""
    try:
        record = change_store.finalize_postmortem(change_id, payload.markdown, actor=principal.actor)
    except ConcurrentModificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        status = 404 if str(exc) == "Change not found" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    indexed = memory_sync.incident(record.incident_id) if memory_sync is not None else 0
    return {"change_id": change_id, "revision": record.postmortem["revision"], "finalized_by": record.postmortem["finalized_by"],
            "memory_documents": indexed}


@app.get("/v1/metrics/summary", response_model=MetricsSummary)
def get_metrics_summary(principal: Principal = Depends(viewer_auth)) -> MetricsSummary:
    _ = principal
    changes = change_store.list_changes()
    return build_metrics_summary(
        changes=changes,
        min_confidence_for_prod=settings.min_confidence_for_prod,
    )


@app.get("/v1/scenarios")
def list_scenarios(principal: Principal = Depends(viewer_auth)) -> list[dict]:
    """Built-in simulated incidents, for trying the agents without a real alert."""
    _ = principal
    return [
        {"name": item.name, "service": item.service, "alert": item.alert_message, "tags": list(item.tags)}
        for item in DEFAULT_SCENARIOS
    ]


@app.post("/v1/incidents/scenario/{name}", response_model=IncidentResponse)
def create_scenario_incident(name: str, principal: Principal = Depends(approver_auth)) -> IncidentResponse:
    """Open a change from a simulated incident. Refused unless the metrics tool is also simulated."""
    _ = principal
    if tool_modes.get("metrics") != "mock":
        raise HTTPException(status_code=409, detail="Scenario incidents are simulations and are disabled when real metrics are configured")
    scenario = next((item for item in DEFAULT_SCENARIOS if item.name == name), None)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Unknown scenario")
    return pipeline.process_event(event_for(scenario))


@app.post("/v1/changes/{change_id}/investigate", status_code=202)
def start_agent_run(change_id: str, principal: Principal = Depends(change_auth("run_agents", APPROVER_ROLES))) -> dict:
    """Run the Investigator, Fixer, Verifier and Critic for this change, in the background."""
    if settings.agent_run_mode == "off":
        raise HTTPException(status_code=409, detail="Agent runs are disabled (AGENT_RUN_MODE=off)")
    if settings.agent_run_mode == "shadow":
        raise HTTPException(
            status_code=409,
            detail="Agents are in shadow mode (AGENT_RUN_MODE=shadow): they observe and report, and nothing they produce enters approval",
        )
    try:
        record, _ = agent_runner.start(change_id, actor=principal.actor)
    except ValueError as exc:
        message = str(exc)
        if message == "Change not found":
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=409, detail=message) from exc
    return {
        "change_id": record.change_id,
        "agent_run_status": record.agent_run_status,
        "deployment_state": record.deployment_state,
        "run": record.agent_run.get("run"),
    }


@app.post("/v1/changes/{change_id}/shadow-run", status_code=202)
def start_shadow_run(change_id: str, principal: Principal = Depends(change_auth("shadow_run", APPROVER_ROLES))) -> dict:
    """Run the agents on this change in observe-only mode (AGENT_RUN_MODE=shadow). Never touches its state."""
    if settings.agent_run_mode != "shadow":
        raise HTTPException(status_code=409, detail="Shadow runs need AGENT_RUN_MODE=shadow")
    try:
        record, _ = agent_runner.start_shadow(change_id, actor=principal.actor)
    except ValueError as exc:
        message = str(exc)
        if message == "Change not found":
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=409, detail=message) from exc
    return {"change_id": record.change_id, "shadow_status": record.shadow.get("status"), "run": record.shadow.get("run")}


@app.get("/v1/shadow/report")
def get_shadow_report(principal: Principal = Depends(viewer_auth)) -> dict:
    """What the agents would have done on past incidents, set against what people did."""
    _ = principal
    return build_shadow_report(change_store.list_changes())


@app.get("/v1/changes/{change_id}/agent-run")
def get_agent_run(change_id: str, principal: Principal = Depends(viewer_auth)) -> dict:
    _ = principal
    record = change_store.get_change(change_id)
    if not record:
        raise HTTPException(status_code=404, detail="Change not found")
    return {
        "change_id": record.change_id,
        "agent_run_status": record.agent_run_status,
        "deployment_state": record.deployment_state,
        "agent_run": record.agent_run,
    }


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
    principal: Principal = Depends(change_auth("execute_dev", APPROVER_ROLES)),
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
def refresh_build_status(change_id: str, principal: Principal = Depends(change_auth("refresh_status", APPROVER_ROLES))) -> ChangeRecord:
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
    principal: Principal = Depends(change_auth("prepare_pr", APPROVER_ROLES)),
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
    principal: Principal = Depends(change_auth("decide", APPROVER_ROLES)),
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
    principal: Principal = Depends(change_auth("promote", {"release_operator"})),
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
