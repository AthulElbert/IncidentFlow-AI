"""What shadow mode has shown so far: what the agents would have done, set against what people actually did.

Humans never see a shadow run's proposal, so this is a comparison and not a score. "The agents would have
proposed a verified fix and a person released a change to the same files" is evidence; a bare count of agreeing
cases is not a measure of how good the agents are, and the report says so instead of reducing it to one number.
"""
from collections import Counter
from typing import Any

from app.models.schemas import ChangeRecord

CAVEAT = (
    "People never saw the agents' proposals in shadow mode, so agreement is a proxy: it compares the files the agents "
    "would have changed with the files a person released, and the agents' diagnosis with the keyword triage."
)


def _human_outcome(record: ChangeRecord) -> str:
    if record.deployment_state == "promoted_to_prod":
        return "released"
    if record.status == "rejected":
        return "rejected"
    return "undecided"


def _agent_files(shadow: dict[str, Any]) -> list[str]:
    if shadow.get("changed_files"):
        return list(shadow["changed_files"])
    attempts = shadow.get("attempts") or []
    return list(((attempts[-1].get("fix") or {}).get("files") or [])) if attempts else []


def build_shadow_report(changes: list[ChangeRecord]) -> dict[str, Any]:
    finished = [item for item in changes if item.shadow.get("status") in {"ready", "escalated"}]
    matrix: dict[str, Counter] = {"fix_ready": Counter(), "escalated": Counter()}
    agreement = {"compared": 0, "same": 0}
    files = {"compared": 0, "same": 0, "overlapping": 0, "different": 0}
    details: list[dict[str, Any]] = []

    for record in finished:
        shadow = record.shadow
        agent = "fix_ready" if shadow["status"] == "ready" else "escalated"
        human = _human_outcome(record)
        matrix[agent][human] += 1

        hypotheses = (shadow.get("investigation") or {}).get("hypotheses") or []
        agent_type = hypotheses[0].get("issue_type") if hypotheses else None
        if agent_type:
            agreement["compared"] += 1
            agreement["same"] += int(agent_type == record.issue_type)

        agent_files, human_files = set(_agent_files(shadow)), set(record.changed_files)
        comparison = None
        if human == "released" and agent_files and human_files:
            files["compared"] += 1
            comparison = "same" if agent_files == human_files else ("overlapping" if agent_files & human_files else "different")
            files[comparison] += 1
        details.append(
            {
                "change_id": record.change_id,
                "service": record.service,
                "agent": agent,
                "human": human,
                "agent_issue_type": agent_type,
                "triage_issue_type": record.issue_type,
                "agent_files": sorted(agent_files),
                "human_files": sorted(human_files),
                "files": comparison,
                "escalation_reason": shadow.get("escalation_reason", ""),
            }
        )

    return {
        "runs": len(finished),
        "running": sum(1 for item in changes if item.shadow.get("status") == "running"),
        "agent_would": {"propose_a_verified_fix": sum(matrix["fix_ready"].values()), "escalate": sum(matrix["escalated"].values())},
        "against_what_people_did": {agent: dict(counts) for agent, counts in matrix.items()},
        "diagnosis_agreement_with_triage": agreement,
        "files": files,
        "changes": details,
        "caveat": CAVEAT,
    }
