"""What a person needs to decide about a change, in one place.

The dashboard asks for this instead of working it out from raw records: what the agents found, what they
changed, how it was checked, which actions are open to a person and, for each closed one, why. Every
"may this happen" answer comes from the same check the store applies when the action is attempted, so
the screen cannot offer something the server would refuse for a different reason.
"""
from collections import Counter
from typing import Any

from app.audit import AuditEntry
from app.models.schemas import ChangeRecord
from app.services.change_control import (
    ChangeControlStore,
    agent_start_blocker,
    decision_blocker,
)

ACTOR_NAMES = {
    "agent:investigator": "Investigator",
    "agent:fixer": "Fixer",
    "agent:verifier": "Verifier",
    "agent:critic": "Critic",
    "agent:orchestrator": "Agent run",
    "system": "System",
    "pipeline": "Intake",
    "scheduler-agent": "Alert correlation",
}

# Plain-language wording for audit actions. Anything not listed falls back to the action name.
ACTION_LABELS = {
    "change.created": "Incident opened",
    "alert.correlated": "Another alert joined this incident",
    "agent.started": "Agent run started",
    "agent.investigated": "Diagnosis reached",
    "agent.fixing": "Fixer working",
    "agent.verifying": "Verifier checking",
    "agent.verified": "Verification finished",
    "agent.reviewing": "Critic reviewing",
    "agent.reviewed": "Review finished",
    "agent.escalated": "Handed to a person",
    "agent.ready_for_approval": "Verified fix ready for approval",
    "agent.interrupted": "Run interrupted by a restart",
    "agent.failed": "Run failed unexpectedly",
    "dev.recorded": "Dev validation recorded",
    "pr.prepared": "Draft PR prepared",
    "decision.approved": "Approved",
    "decision.rejected": "Rejected",
    "promotion.reserved": "Promotion started",
    "promotion.success": "Promoted to production",
    "promotion.failed": "Production deploy failed",
    "promotion.queued": "Production deploy running",
}

_TERMINAL = {"promoted_to_prod", "prod_promotion_failed", "closed_rejected", "rolled_back", "resolved"}


def attention(record: ChangeRecord) -> str | None:
    """What this change is waiting for, if anything. Drives the dashboard's inbox."""
    if record.agent_run_status == "running":
        return "agents_working"
    if record.deployment_state == "promoting":
        return "promoting"
    if record.deployment_state == "blocked_by_policy":
        return "blocked"
    if record.deployment_state == "escalated":
        return "escalated"
    if record.deployment_state == "ready_for_prod":
        return "needs_approval" if record.status == "pending_approval" else "needs_promotion"
    return None


def overview_row(record: ChangeRecord) -> dict[str, Any]:
    """The small record the dashboard polls. `version` changes whenever anything about the change does."""
    return {
        "change_id": record.change_id,
        "service": record.service,
        "issue_type": record.issue_type,
        "confidence": record.confidence,
        "status": record.status,
        "deployment_state": record.deployment_state,
        "agent_run_status": record.agent_run_status,
        "pr_status": record.pr_status,
        "dev_execution_status": record.dev_execution_status,
        "triage_mode_used": record.triage_mode_used,
        "alert_count": record.alert_count,
        "created_at": record.created_at,
        "version": record.version,
        "attention": attention(record),
    }


# ------------------------------------------------------------------------------------ timeline


# Actors that are the platform itself, not a model and not a person.
SYSTEM_ACTORS = {"system", "pipeline", "scheduler-agent", "auth-disabled"}


def _actor_kind(actor: str) -> str:
    if actor.startswith("agent:"):
        return "agent"
    return "system" if actor in SYSTEM_ACTORS else "human"


def _actor_name(actor: str) -> str:
    return ACTOR_NAMES.get(actor, actor)


def _tool_summary(group: list[AuditEntry]) -> str:
    counts = Counter(entry.action.removeprefix("tool.").removesuffix(".denied") for entry in group)
    return ", ".join(f"{name} x{count}" if count > 1 else name for name, count in counts.items())


def build_timeline(entries: list[AuditEntry]) -> list[dict[str, Any]]:
    """Audit entries in plain language, oldest first. Runs of tool calls by one agent become a single line."""
    timeline: list[dict[str, Any]] = []
    group: list[AuditEntry] = []

    def flush() -> None:
        if not group:
            return
        denied = sum(1 for entry in group if entry.action.endswith(".denied"))
        failed = sum(1 for entry in group if entry.details.get("ok") is False)
        label = f"used {len(group)} tool call{'s' if len(group) != 1 else ''}"
        timeline.append(
            {
                "at": group[0].occurred_at,
                "actor": group[0].actor,
                "actor_name": _actor_name(group[0].actor),
                "actor_kind": _actor_kind(group[0].actor),
                "kind": "tools",
                "label": label,
                "note": _tool_summary(group),
                "problem": f"{denied} denied" if denied else (f"{failed} failed" if failed else ""),
            }
        )
        group.clear()

    for entry in entries:
        if entry.action.startswith("tool."):
            if group and group[0].actor != entry.actor:
                flush()
            group.append(entry)
            continue
        flush()
        details = entry.details or {}
        note = str(details.get("note") or details.get("reason") or details.get("comment") or "")
        timeline.append(
            {
                "at": entry.occurred_at,
                "actor": entry.actor,
                "actor_name": _actor_name(entry.actor),
                "actor_kind": _actor_kind(entry.actor),
                "kind": "state" if entry.from_state != entry.to_state else "event",
                "label": ACTION_LABELS.get(entry.action, entry.action),
                "from_state": entry.from_state,
                "to_state": entry.to_state,
                "note": note,
                "problem": "",
            }
        )
    flush()
    return timeline


# -------------------------------------------------------------------------------------- actions


def _action(name: str, label: str, role: str, blocker: str | None, note: str = "") -> dict[str, Any]:
    return {"name": name, "label": label, "role": role, "allowed": blocker is None, "reason": blocker or "", "note": note}


def build_actions(record: ChangeRecord, store: ChangeControlStore, agent_mode: str) -> list[dict[str, Any]]:
    """The actions a person could take on this change right now, each with the reason when it cannot be."""
    working = "The agents are still working on this change" if record.agent_run_status == "running" else None

    run_blocker = "Agent runs are disabled (AGENT_RUN_MODE=off)" if agent_mode == "off" else agent_start_blocker(record)
    approve_blocker = working or decision_blocker(record, "approve")
    reject_blocker = decision_blocker(record, "reject")
    promote_blocker = working or store.promotion_blocker(record)
    refresh_blocker = (
        None if record.deployment_state in {"dev_fix_in_progress", "promoting"} else "No build is in progress for this change"
    )

    promote_note = ""
    if store.policy.enforce_separation_of_duties and record.status == "approved" and record.decided_by:
        promote_note = f"Must be done by someone other than {record.decided_by}, who approved it"

    return [
        _action("run_agents", "Run agents", "approver", run_blocker),
        _action("approve", "Approve", "approver", approve_blocker),
        _action("reject", "Reject", "approver", reject_blocker),
        _action("promote", "Promote to production", "release_operator", promote_blocker, promote_note),
        _action("refresh_status", "Refresh build status", "approver", refresh_blocker),
    ]


# ---------------------------------------------------------------------------------- the case file


def _headline(record: ChangeRecord, run: dict[str, Any]) -> str:
    state = record.deployment_state
    if record.status == "rejected":
        return f"Rejected by {record.decided_by or 'a person'}"
    if state == "promoted_to_prod":
        return "Promoted to production"
    if state == "prod_promotion_failed":
        return "The production deploy failed"
    if state == "promoting":
        return "Production deploy in progress"
    if state == "blocked_by_policy":
        return "Approved, but the policy gate blocks promotion"
    if record.agent_run_status == "running":
        return "The agents are working on this change"
    if state == "escalated":
        return "The agents could not fix this and handed it to a person"
    if state == "ready_for_prod" and record.status == "approved":
        return f"Approved by {record.decided_by or 'a person'}; waiting for a different release operator to promote it"
    if state == "ready_for_prod":
        if run.get("diff"):
            return "A verified fix is waiting for your approval"
        return "Dev validation passed; waiting for approval"
    if state == "awaiting_dev_execution":
        return "Not investigated yet"
    return state.replace("_", " ").capitalize()


def _diagnosis(run: dict[str, Any]) -> dict[str, Any] | None:
    investigation = run.get("investigation")
    if not investigation or not investigation.get("hypotheses"):
        return None
    top, *others = investigation["hypotheses"]
    return {
        "issue_type": top.get("issue_type"),
        "confidence": top.get("confidence"),
        "root_cause": top.get("root_cause"),
        "mode": investigation.get("mode_used"),
        "alternatives": [{"issue_type": item.get("issue_type"), "confidence": item.get("confidence")} for item in others],
        "decision": investigation.get("decision"),
        "escalation_reason": investigation.get("escalation_reason", ""),
    }


def _evidence(run: dict[str, Any]) -> list[dict[str, Any]]:
    investigation = run.get("investigation") or {}
    hypotheses = investigation.get("hypotheses") or []
    supporting = set(hypotheses[0].get("supporting", [])) if hypotheses else set()
    return [
        {
            "id": item.get("id"),
            "kind": item.get("kind"),
            "summary": item.get("summary"),
            "source": item.get("source", ""),
            "supports_diagnosis": item.get("id") in supporting,
        }
        for item in investigation.get("evidence", [])
    ]


def _fix(run: dict[str, Any]) -> dict[str, Any] | None:
    attempts = run.get("attempts") or []
    last_fix = (attempts[-1].get("fix") if attempts else None) or {}
    if last_fix.get("status") != "proposed":
        return None
    return {
        "branch": run.get("branch", ""),
        "files": run.get("changed_files") or last_fix.get("files", []),
        "summary": last_fix.get("summary", ""),
        "attempts": len(attempts),
        "diff": run.get("diff"),
    }


def _verification(run: dict[str, Any]) -> dict[str, Any] | None:
    report = run.get("verification")
    if not report:
        return None
    return {
        "verdict": report.get("verdict"),
        "checks": [
            {"name": check.get("name"), "status": check.get("status"), "detail": check.get("detail", "")}
            for check in report.get("checks", [])
        ],
        "alert_metric": report.get("alert_metric", {}),
        "warnings": report.get("warnings", []),
    }


def _critic(run: dict[str, Any]) -> dict[str, Any] | None:
    review = run.get("review")
    if not review:
        return None
    return {
        "decision": review.get("decision"),
        "risk": review.get("risk"),
        "mode": review.get("mode_used"),
        "changed_lines": review.get("changed_lines", 0),
        "concerns": [
            {"severity": item.get("severity"), "text": item.get("text")}
            for item in review.get("concerns", [])
            if item.get("severity") != "info"
        ],
    }


def build_review(
    record: ChangeRecord,
    entries: list[AuditEntry],
    store: ChangeControlStore,
    agent_mode: str,
) -> dict[str, Any]:
    run = record.agent_run or {}
    reasons = store.policy_reasons(record)
    return {
        "change_id": record.change_id,
        "version": record.version,
        "service": record.service,
        "headline": _headline(record, run),
        "attention": attention(record),
        "terminal": record.deployment_state in _TERMINAL,
        "agent_run": {
            "status": record.agent_run_status,
            "run": run.get("run"),
            "escalation_reason": run.get("escalation_reason", ""),
            "summary": run.get("summary", ""),
        },
        "diagnosis": _diagnosis(run),
        "evidence": _evidence(run),
        "fix": _fix(run),
        "verification": _verification(run),
        "critic": _critic(run),
        "policy": {"passes": not reasons, "reasons": reasons},
        "approval": {
            "status": record.status,
            "decided_by": record.decided_by,
            "decided_at": record.decided_at,
            "comment": record.decision_comment,
            "promoted_by": record.promoted_by,
            "promotion_result": record.promotion_result,
            "prod_deploy_url": record.prod_deploy_url,
            "enforce_separation_of_duties": store.policy.enforce_separation_of_duties,
        },
        "actions": build_actions(record, store, agent_mode),
        "timeline": build_timeline(entries),
    }
