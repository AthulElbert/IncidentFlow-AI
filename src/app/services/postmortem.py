"""A postmortem draft, written from what the platform recorded. A person edits and owns the final version.

It is built only from the change's case file (see `review.build_review`), so it states facts and leaves the
judgement ("was the root cause right?", "what should change?") as prompts for the people who were there.
"""
from datetime import datetime
from typing import Any

from app.models.schemas import ChangeRecord

MAX_DIFF_LINES = 80


def _when(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(value, datetime) else ""


def _duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return ""
    seconds = int((end - start).total_seconds())
    minutes, secs = divmod(max(seconds, 0), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _follow_ups(record: ChangeRecord, review: dict[str, Any]) -> list[str]:
    items = ["Was the diagnosis right? Correct it here if the real cause was different."]
    diagnosis = review.get("diagnosis")
    if diagnosis and (diagnosis.get("confidence") or 0) < 0.8:
        items.append(f"The agents' confidence was only {diagnosis['confidence']:.2f}; note what confirmed or contradicted it.")
    for concern in (review.get("critic") or {}).get("concerns", []):
        items.append(f"The Critic raised a {concern['severity']} concern: {concern['text']} Was it addressed?")
    runs = (review.get("agent_run") or {}).get("run") or 0
    if runs > 1:
        items.append(f"It took {runs} agent runs. What stopped the earlier ones?")
    if record.deployment_state != "promoted_to_prod":
        items.append("The change did not reach production; record what happened to the incident instead.")
    items.append("What would have caught this earlier (alert, test, review)?")
    return items


def build_postmortem(record: ChangeRecord, review: dict[str, Any]) -> str:
    alert = record.alert or {}
    lines = [
        f"# Postmortem draft: {record.service} ({record.change_id})",
        "",
        f"*Draft generated from the platform's records. Status: {review['headline']}. A person must review and edit it.*",
        "",
        "## Summary",
        f"- Service / environment: {record.service} / {record.environment}",
        f"- Alert: {alert.get('metric', 'unknown')} was {alert.get('value', '?')} against a threshold of {alert.get('threshold', '?')}"
        + (f' ("{alert["message"]}")' if alert.get("message") else ""),
        f"- Opened: {_when(record.created_at)}",
    ]
    if record.promoted_at:
        lines.append(f"- Released to production: {_when(record.promoted_at)} ({_duration(record.created_at, record.promoted_at)} after the alert)")
    if record.alert_count > 1:
        lines.append(f"- Alerts folded into this incident: {record.alert_count}")

    diagnosis = review.get("diagnosis")
    lines += ["", "## Diagnosis"]
    if diagnosis:
        lines.append(f"- **{diagnosis['issue_type']}**, confidence {diagnosis['confidence']:.2f} ({diagnosis['mode']}): {diagnosis['root_cause']}")
        supporting = [item for item in review.get("evidence", []) if item["supports_diagnosis"]]
        for item in supporting:
            lines.append(f"  - evidence {item['id']} ({item['kind']}): {item['summary']}")
    else:
        lines.append("- The agents did not reach a diagnosis for this change.")

    fix = review.get("fix")
    lines += ["", "## What was changed"]
    if fix:
        lines.append(f"- Files: {', '.join(fix['files']) or 'none'} (branch `{fix['branch']}`, verified on attempt {fix['attempts']})")
        if fix.get("summary"):
            lines.append(f"- Summary: {fix['summary']}")
        diff = (fix.get("diff") or {}).get("text", "")
        if diff:
            shown = diff.splitlines()[:MAX_DIFF_LINES]
            lines += ["", "```diff", *shown, "```"]
            if len(diff.splitlines()) > MAX_DIFF_LINES or fix["diff"].get("truncated"):
                lines.append("*(diff shortened; see the branch for the full change)*")
    else:
        lines.append("- No agent-produced change; any fix was prepared by the manual flow.")

    verification = review.get("verification")
    lines += ["", "## How it was checked"]
    if verification:
        lines.append(f"- Verifier verdict: **{verification['verdict']}**")
        for check in verification["checks"]:
            lines.append(f"  - {check['name']}: {check['status']}")
        metric = verification.get("alert_metric") or {}
        if "before" in metric and "after" in metric:
            lines.append(f"- Alert metric in dev: {metric['before']} before, {metric['after']} after (threshold {metric.get('threshold')})")
    else:
        lines.append("- No agent verification was recorded.")
    critic = review.get("critic")
    if critic:
        lines.append(f"- Critic: {critic['decision']}, risk {critic['risk']}")

    approval = review["approval"]
    lines += ["", "## Decisions"]
    if approval.get("decided_by"):
        lines.append(f"- {approval['status'].capitalize()} by {approval['decided_by']} at {_when(approval.get('decided_at'))}"
                     + (f': "{approval["comment"]}"' if approval.get("comment") else ""))
    else:
        lines.append("- No decision was recorded.")
    if approval.get("promoted_by"):
        lines.append(f"- Released by {approval['promoted_by']} (result: {approval.get('promotion_result')})")

    lines += ["", "## Timeline"]
    for entry in review.get("timeline", []):
        note = f" ({entry['note']})" if entry.get("note") else ""
        lines.append(f"- {_when(entry['at'])} {entry['actor_name']}: {entry['label']}{note}")

    lines += ["", "## For the people involved to fill in", *[f"- [ ] {item}" for item in _follow_ups(record, review)]]
    return "\n".join(lines) + "\n"
