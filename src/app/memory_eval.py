"""A retrieval check for incident memory: does a search find the right past incident, and only when there is one?

The corpus is the replay harness' four past incidents plus three distinct seed incidents. Each case describes a
past incident in *different words* from how it was recorded (the case semantic search should help with), or
describes something no past incident covers (which must match nothing: a false match raises a diagnosis'
confidence). Written together with the code it checks, so it is a sanity check and a way to choose the similarity
floor, not an accuracy figure for real incidents.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models.schemas import IncidentRecord
from app.tools.scenarios import eval_history

# The evidence threshold the Investigator applies to a memory match (agents/evidence.MIN_SIMILARITY).
from app.agents.evidence import MIN_SIMILARITY


def _seed(incident_id, service, metric, issue_type, summary, resolution) -> IncidentRecord:
    return IncidentRecord(incident_id=incident_id, service=service, metric=metric, issue_type=issue_type, summary=summary,
                          resolution=resolution, created_at=datetime(2026, 5, 1, tzinfo=timezone.utc))


def corpus() -> list[IncidentRecord]:
    return [
        *eval_history(),
        _seed("INC-DEMO0001", "payments-service", "latency_p99_ms", "performance_degradation",
              "payments-service - performance_degradation in prod",
              "Increased connection pool size and enabled response caching; latency returned to baseline within 15 minutes."),
        _seed("INC-DEMO0002", "orders-service", "error_rate_pct", "application_error",
              "orders-service - application_error in prod",
              "Rolled back the faulty deployment and enabled the safe-mode feature flag for the checkout module."),
        _seed("INC-DEMO0003", "checkout-service", "dependency_timeout_ms", "dependency_failure",
              "checkout-service - dependency_failure in prod",
              "Adjusted upstream timeout and connection pool settings; downstream dependency stabilized."),
    ]


@dataclass(frozen=True)
class Case:
    query: str
    expected: frozenset[str] = field(default_factory=frozenset)  # empty: nothing should match


CASES: tuple[Case, ...] = (
    # Different words from the record (the case for semantic search)
    Case("the ledger database ran out of available connections and calls to it kept timing out", frozenset({"INC-H2", "INC-DEMO0003"})),
    Case("a worker used up all its RAM and kubernetes kept killing the pods", frozenset({"INC-H3"})),
    Case("requests became slow right after somebody changed a setting", frozenset({"INC-H4"})),
    Case("500s started after the new release; a null reference in the request handler", frozenset({"INC-H1"})),
    Case("card payments are slow because the database connections are saturated", frozenset({"INC-DEMO0001"})),
    Case("orders app throwing errors after the latest release; we switched on a fallback flag", frozenset({"INC-DEMO0002", "INC-H1"})),
    Case("checkout cannot reach a downstream system and requests to it time out", frozenset({"INC-DEMO0003", "INC-H2"})),
    # Shared words (keyword search should already manage these)
    Case("connection pool exhausted timeouts", frozenset({"INC-H2", "INC-DEMO0003"})),
    Case("OutOfMemoryError heap OOMKilled", frozenset({"INC-H3"})),
    # Nothing in memory covers these: any match is a false one
    Case("the TLS certificate on the marketing website expired"),
    Case("users cannot reset their passwords from the login page"),
    Case("the quarterly sales report needs a new chart"),
    Case("disk on the build agent is full so CI jobs fail to start"),
)


@dataclass
class RetrievalResult:
    found_rate: float  # of answerable cases, the share with an expected incident in the top 3 counted matches
    mean_reciprocal_rank: float
    false_matches: int  # unanswerable cases that returned any counted match
    wrong_top: int  # answerable cases whose first counted match is not an expected incident
    rows: list[dict]


def evaluate(memory, k: int = 3) -> RetrievalResult:
    answerable = [c for c in CASES if c.expected]
    found = reciprocal = wrong_top = false = 0
    rows = []
    for case in CASES:
        counted = [r for r in memory.search(case.query, k=k) if r.score >= MIN_SIMILARITY]
        ids = [r.incident_id for r in counted]
        rank = next((i + 1 for i, incident in enumerate(ids) if incident in case.expected), None)
        if case.expected:
            found += rank is not None
            reciprocal += 1.0 / rank if rank else 0.0
            wrong_top += bool(ids) and ids[0] not in case.expected
        else:
            false += bool(ids)
        rows.append({"query": case.query, "expected": sorted(case.expected), "got": [(r.incident_id, r.score) for r in counted]})
    return RetrievalResult(
        found_rate=round(found / len(answerable), 3),
        mean_reciprocal_rank=round(reciprocal / len(answerable), 3),
        false_matches=false,
        wrong_top=wrong_top,
        rows=rows,
    )


class CachingEmbedder:
    """Embeds each text once, so the same eval can be scored at many floors without new API calls."""

    def __init__(self, inner) -> None:
        self.inner, self.model, self.dimensions = inner, inner.model, inner.dimensions
        self._cache: dict[str, list[float]] = {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            self._cache.update(zip(missing, self.inner.embed(missing)))
        return [self._cache[t] for t in texts]
