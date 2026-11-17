"""Choosing more evidence from what the scoring says is missing.

`scoring` knows, for each possible cause, which signals it would expect and which were not observed. When an
investigation is about to hand an incident to a person (low confidence, two causes too close, or no cause), this
turns those missing signals into the tool calls that could confirm or rule them out, most valuable first.

Every possible cause is considered, not only the strongest: when the evidence is thin every cause scores near
zero, and "the two strongest" would be an arbitrary pick. Follow-ups are ordered metrics first (numbers the plan
structurally could not see), then log searches, then memory, and by the weight of the missing signal within each.

It only asks for what the fixed evidence plan could not have seen:
- a metric of the expected kind that the plan did not query (it queries the alert metric and five others);
- log lines of the expected kind, searched for by keyword over a longer window, so that a busy service's
  newer lines cannot push them out of the plan's 200-line sample;
- past incidents of the expected kind, searched for by that kind rather than by the alert's wording.

Deploys are not followed up: the plan already looks back 180 minutes, far longer than a deploy can lead an onset
and still count. Everything here is deterministic, so a replay gives the same follow-ups every time.
"""
import json
from dataclasses import dataclass, field

from app.agents.evidence import EvidenceLedger, metric_family
from app.agents.models import IncidentContext
from app.agents.scoring import Candidate, missing_signals

FOLLOW_UP_LOG_MINUTES = 180
# The word to search logs for, per log pattern (see evidence.LOG_PATTERNS); every word of a query must appear.
LOG_QUERIES = {
    "exception": "exception",
    "http5xx": "http",
    "timeout": "timeout",
    "pool": "pool",
    "circuit_breaker": "circuit",
    "oom": "memory",
    "cpu": "throttl",
    "slow": "slow",
    "config_reload": "config",
}


def _log_query_order(tag: str) -> tuple[int, str]:
    """Log tags in the order of LOG_QUERIES (timeout before pool before circuit breaker), others after."""
    names = list(LOG_QUERIES)
    detail = tag.partition(":")[2]
    return (names.index(detail) if detail in names else len(names), tag)


@dataclass(frozen=True)
class FollowUp:
    tool: str
    method: str
    args: dict = field(default_factory=dict)
    why: str = ""
    weight: float = 0.0

    def request(self) -> dict:
        return {"action": "tool", "tool": self.tool, "method": self.method, "args": dict(self.args)}

    def key(self) -> str:
        return json.dumps(self.request(), sort_keys=True)


def plan_follow_ups(
    candidates: dict[str, Candidate], ledger: EvidenceLedger, ctx: IncidentContext, limit: int
) -> list[FollowUp]:
    """The most useful extra tool calls, best first, at most `limit`."""
    if limit <= 0:
        return []
    ranked = sorted(
        (c for c in candidates.values() if c.issue_type != "unknown"), key=lambda c: (-c.confidence, c.issue_type)
    )
    queried = {item.data.get("metric") for item in ledger.items if item.data.get("metric")}
    plans: list[FollowUp] = []

    for candidate in ranked:
        for signal in missing_signals(candidate.issue_type, ledger.items):
            if not signal.specific:
                continue
            why = f"{candidate.issue_type} would show {signal.label}, which has not been seen"
            for tag in sorted(signal.tags, key=_log_query_order):
                prefix, _, detail = tag.partition(":")
                if prefix == "metric_anomaly":
                    for metric in ledger.available_metrics:
                        if metric not in queried and metric_family(metric) == detail:
                            plans.append(FollowUp("metrics", "query", {"metric": metric, "minutes": 60}, why, signal.weight))
                elif prefix == "log" and detail in LOG_QUERIES:
                    plans.append(
                        FollowUp("logs", "search", {"query": LOG_QUERIES[detail], "minutes": FOLLOW_UP_LOG_MINUTES}, why, signal.weight)
                    )
                elif prefix == "similar":
                    text = f"{detail.replace('_', ' ')} {ctx.alert_metric} {ctx.alert_message}"
                    plans.append(FollowUp("memory", "search", {"text": text[:300]}, why, signal.weight))

    unique: dict[str, FollowUp] = {}
    for plan in plans:
        unique.setdefault(plan.key(), plan)
    order = {"metrics": 0, "logs": 1, "memory": 2}
    # Stable sort: within a kind and weight, the stronger cause (planned first) comes first.
    return sorted(unique.values(), key=lambda p: (order.get(p.tool, 3), -p.weight))[:limit]
