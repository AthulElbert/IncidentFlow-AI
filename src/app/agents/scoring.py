"""Evidence-based confidence for each possible issue type.

For an issue type, a fixed set of signals would be expected if it were the cause (weights sum
to 1.0), and a smaller set of signals would count against it. Confidence is the weight of the
signals actually observed, minus the weight of contradicting ones. It depends only on the
evidence and on nothing a model wrote: citing more evidence or claiming certainty cannot raise it.

A hypothesis with no *specific* supporting signal scores 0, so a non-specific fact like "there was
no recent deploy" can never carry a conclusion on its own.
"""
from dataclasses import dataclass, field

from app.agents.models import Evidence

UNKNOWN_CEILING = 0.5


@dataclass(frozen=True)
class Signal:
    label: str
    tags: frozenset[str]  # any of these tags satisfies the signal
    weight: float
    specific: bool = True
    unless: frozenset[str] = frozenset()  # signal is ignored when any of these tags is present


def _s(label: str, tags: str, weight: float, specific: bool = True, unless: str = "") -> Signal:
    return Signal(label, frozenset(tags.split("|")), weight, specific, frozenset(unless.split("|")) if unless else frozenset())


SUPPORT: dict[str, tuple[Signal, ...]] = {
    "application_error": (
        _s("error-rate metric anomaly", "metric_anomaly:error", 0.30),
        _s("code deploy shortly before the onset", "deploy_before_onset:code", 0.25),
        _s("exception or HTTP 5xx in the logs", "log:exception|log:http5xx", 0.30),
        _s("similar past application error", "similar:application_error", 0.15),
    ),
    "performance_degradation": (
        _s("latency metric anomaly", "metric_anomaly:latency", 0.30),
        _s("code or config deploy shortly before the onset", "deploy_before_onset:code|deploy_before_onset:config", 0.25),
        _s("slow responses or a config reload in the logs", "log:slow|log:config_reload", 0.30),
        _s("similar past performance degradation", "similar:performance_degradation", 0.15),
    ),
    "dependency_failure": (
        _s("dependency/timeout metric anomaly", "metric_anomaly:dependency", 0.30),
        _s("timeouts, pool exhaustion or an open circuit breaker in the logs", "log:timeout|log:pool|log:circuit_breaker", 0.35),
        _s("no recent deploy", "no_deploy", 0.15, specific=False),
        _s("similar past dependency failure", "similar:dependency_failure", 0.20),
    ),
    "resource_saturation": (
        _s("CPU/memory/disk metric anomaly", "metric_anomaly:resource", 0.40),
        _s("out-of-memory or CPU throttling in the logs", "log:oom|log:cpu", 0.30),
        _s("no recent deploy", "no_deploy", 0.10, specific=False),
        _s("similar past resource saturation", "similar:resource_saturation", 0.20),
    ),
}

CONTRADICT: dict[str, tuple[Signal, ...]] = {
    "application_error": (
        _s("no recent deploy", "no_deploy", 0.05, specific=False),
        _s("timeouts or pool exhaustion in the logs", "log:timeout|log:pool", 0.10),
        _s("error-rate metric is normal", "metric_normal:error", 0.20, unless="metric_anomaly:error"),
    ),
    "performance_degradation": (
        _s("timeouts or pool exhaustion in the logs", "log:timeout|log:pool", 0.10),
        _s("latency metric is normal", "metric_normal:latency", 0.30, unless="metric_anomaly:latency"),
    ),
    "dependency_failure": (
        _s("code or config deploy shortly before the onset", "deploy_before_onset:code|deploy_before_onset:config", 0.10),
        _s("dependency metric is normal", "metric_normal:dependency", 0.20, unless="metric_anomaly:dependency"),
    ),
    "resource_saturation": (
        _s("resource metrics are normal", "metric_normal:resource", 0.35, unless="metric_anomaly:resource"),
    ),
}

ISSUE_TYPES_SCORED = tuple(SUPPORT)


@dataclass
class Candidate:
    issue_type: str
    confidence: float
    supporting: list[str] = field(default_factory=list)
    contradicting: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _matching(evidence: list[Evidence], signal: Signal) -> list[Evidence]:
    if signal.unless and any(signal.unless & set(item.tags) for item in evidence):
        return []
    return [item for item in evidence if signal.tags & set(item.tags)]


def score_issue_type(issue_type: str, evidence: list[Evidence]) -> Candidate:
    support = 0.0
    has_specific = False
    candidate = Candidate(issue_type=issue_type, confidence=0.0)

    for signal in SUPPORT[issue_type]:
        matched = _matching(evidence, signal)
        if matched:
            support += signal.weight
            has_specific = has_specific or signal.specific
            candidate.supporting.extend(item.id for item in matched)
        else:
            candidate.missing.append(signal.label)

    penalty = 0.0
    for signal in CONTRADICT.get(issue_type, ()):
        matched = _matching(evidence, signal)
        if matched:
            penalty += signal.weight
            candidate.contradicting.extend(item.id for item in matched)

    candidate.supporting = sorted(set(candidate.supporting), key=_order)
    candidate.contradicting = sorted(set(candidate.contradicting), key=_order)
    candidate.confidence = round(min(1.0, max(0.0, support - penalty)), 3) if has_specific else 0.0
    return candidate


def score_all(evidence: list[Evidence]) -> dict[str, Candidate]:
    """Score every issue type, including `unknown`, which rises as the others fall."""
    scored = {name: score_issue_type(name, evidence) for name in ISSUE_TYPES_SCORED}
    strongest = max(candidate.confidence for candidate in scored.values())
    scored["unknown"] = Candidate(
        issue_type="unknown",
        confidence=round(min(UNKNOWN_CEILING, UNKNOWN_CEILING * (1.0 - strongest)), 3),
        missing=["any signal that points to a specific cause"],
    )
    return scored


def _order(evidence_id: str) -> int:
    return int(evidence_id[1:]) if evidence_id[1:].isdigit() else 0
