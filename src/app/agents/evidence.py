"""Turns raw tool output into tagged, citable evidence.

Everything here is deterministic. A metric is "anomalous" because of arithmetic on its
points, a deploy "preceded the onset" because of timestamps, a log "shows a timeout"
because of a pattern match. No model decides any of it, which is what lets the scoring
module trust the tags.
"""
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from app.agents.models import Evidence
from app.tools.models import DeployRecord, LogLine, MetricSeries, ScoredIncident

MIN_POINTS = 8
RATIO_THRESHOLD = 1.8
PERCENT_POINTS_THRESHOLD = 20.0
DEPLOY_LEAD_MINUTES = 30
MIN_SIMILARITY = 0.30
_SAMPLE_CHARS = 240

_METRIC_FAMILIES = (
    ("dependency", re.compile(r"dependency|upstream|downstream|\bdb\b|database|connection|pool|timeout|queue|cache", re.I)),
    ("resource", re.compile(r"cpu|memory|\bmem\b|heap|disk|\bgc\b|thread|saturation|utilization", re.I)),
    ("latency", re.compile(r"latency|p9\d|response_time|duration|seconds", re.I)),
    ("error", re.compile(r"error|5xx|fail|exception", re.I)),
)

# name -> (regex, short label). Matched against "<LEVEL> <message>" lower-cased.
LOG_PATTERNS: dict[str, re.Pattern[str]] = {
    "exception": re.compile(r"exception|traceback|stack ?trace|nullpointer|panic|segfault"),
    "http5xx": re.compile(r"http 5\d\d|status[=: ]5\d\d|returned 5\d\d"),
    "timeout": re.compile(r"time[d ]?-?out"),
    "pool": re.compile(r"pool exhausted|connection pool|too many connections"),
    "circuit_breaker": re.compile(r"circuit breaker"),
    "oom": re.compile(r"out ?of ?memory|outofmemory|\boom\b|oomkilled|gc overhead|java heap"),
    "cpu": re.compile(r"cpu (throttl|saturat)|throttl"),
    "slow": re.compile(r"\bslow\b|elapsed_ms|duration_ms=\d{4,}"),
    "config_reload": re.compile(r"config reloaded|configuration (changed|reloaded)|latency_ms="),
}


def metric_family(metric: str) -> str:
    for family, pattern in _METRIC_FAMILIES:
        if pattern.search(metric):
            return family
    return "other"


def clean_text(text: str, limit: int = _SAMPLE_CHARS) -> str:
    """Strip control characters and cap the length of untrusted text before it is stored or shown."""
    flattened = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return flattened if len(flattened) <= limit else flattened[:limit] + "..."


def _clock(moment: datetime) -> str:
    return moment.strftime("%H:%M UTC")


@dataclass(frozen=True)
class MetricAnalysis:
    anomalous: bool
    enough_data: bool
    baseline: float
    recent: float
    ratio: float | None
    onset: datetime | None


def analyze_series(series: MetricSeries) -> MetricAnalysis:
    points = series.points
    if len(points) < MIN_POINTS:
        return MetricAnalysis(False, False, 0.0, 0.0, None, None)

    values = [point.value for point in points]
    count = len(values)
    baseline = statistics.median(values[: max(3, int(count * 0.4))])
    tail = sorted(values[-max(3, int(count * 0.25)):])
    recent = tail[min(len(tail) - 1, int(len(tail) * 0.9))]
    delta = recent - baseline
    ratio = (recent / baseline) if baseline > 0 else None

    rose_in_ratio = delta > 0 and (ratio is None or ratio >= RATIO_THRESHOLD)
    rose_in_points = series.unit == "percent" and delta >= PERCENT_POINTS_THRESHOLD
    anomalous = rose_in_ratio or rose_in_points

    onset = None
    if anomalous:
        midway = baseline + 0.5 * delta
        onset = next((p.timestamp for p in points if p.value >= midway), None)
    return MetricAnalysis(anomalous, True, baseline, recent, ratio, onset)


class EvidenceLedger:
    """Collects evidence for one investigation, assigns ids, and keeps deploy tags consistent."""

    def __init__(self, service: str) -> None:
        self.service = service
        self.items: list[Evidence] = []
        self.warnings: list[str] = []
        self._keys: set[str] = set()

    # ------------------------------------------------------------------ basics

    def _add(self, key: str, **fields) -> Evidence | None:
        if key in self._keys:
            return None
        self._keys.add(key)
        evidence = Evidence(id=f"E{len(self.items) + 1}", **fields)
        self.items.append(evidence)
        return evidence

    def get(self, evidence_id: str) -> Evidence | None:
        return next((item for item in self.items if item.id == evidence_id), None)

    @property
    def onset(self) -> datetime | None:
        onsets = [
            datetime.fromisoformat(item.data["onset"])
            for item in self.items
            if item.kind == "metric_anomaly" and item.data.get("onset")
        ]
        return min(onsets) if onsets else None

    # ----------------------------------------------------------------- metrics

    def add_metric(self, series: MetricSeries, source: str) -> Evidence | None:
        family = metric_family(series.metric)
        analysis = analyze_series(series)
        key = f"metric:{series.service}:{series.metric}"

        if series.service != self.service:
            # Another service's metric (a dependency, say): useful context for a reader,
            # but it must not count as a signal about this incident's service.
            verdict = "rose" if analysis.anomalous else "is normal"
            return self._add(
                key,
                kind="foreign_metric",
                tags=["foreign"],
                summary=f"[{series.service}] {series.metric} {verdict} ({analysis.baseline:.4g} -> {analysis.recent:.4g})",
                data={"metric": series.metric, "service": series.service, "anomalous": analysis.anomalous},
                source=source,
            )

        if not analysis.enough_data:
            self.warnings.append(f"not enough data points for {series.metric}")
            return self._add(
                key,
                kind="metric_no_data",
                summary=f"{series.metric}: fewer than {MIN_POINTS} data points, cannot assess",
                data={"metric": series.metric, "points": len(series.points)},
                source=source,
            )

        unit = f" {series.unit}" if series.unit else ""
        data = {
            "metric": series.metric,
            "family": family,
            "baseline": round(analysis.baseline, 4),
            "recent": round(analysis.recent, 4),
            "ratio": None if analysis.ratio is None else round(analysis.ratio, 2),
        }
        if analysis.anomalous:
            data["onset"] = analysis.onset.isoformat() if analysis.onset else None
            since = f" from about {_clock(analysis.onset)}" if analysis.onset else ""
            created = self._add(
                key,
                kind="metric_anomaly",
                tags=["metric_anomaly", f"metric_anomaly:{family}"],
                summary=(
                    f"{series.metric} rose from {analysis.baseline:.4g} to {analysis.recent:.4g}{unit}{since}"
                ),
                phrase=f"{series.metric} rose from {analysis.baseline:.4g} to {analysis.recent:.4g}{unit}",
                data=data,
                source=source,
            )
            self._reclassify_deploys()
            return created

        return self._add(
            key,
            kind="metric_normal",
            tags=["metric_normal", f"metric_normal:{family}"],
            summary=f"{series.metric} is normal ({analysis.baseline:.4g} -> {analysis.recent:.4g}{unit})",
            data=data,
            source=source,
        )

    # -------------------------------------------------------------------- logs

    def add_logs(self, lines: Iterable[LogLine], source: str, service: str | None = None) -> list[Evidence]:
        svc = service or self.service
        foreign = svc != self.service
        prefix = f"[{svc}] " if foreign else ""
        lines = list(lines)
        created: list[Evidence] = []
        matched: set[int] = set()
        for name, pattern in LOG_PATTERNS.items():
            hits = [(i, line) for i, line in enumerate(lines) if pattern.search(f"{line.level} {line.message}".lower())]
            if not hits:
                continue
            matched.update(i for i, _ in hits)
            samples = _frequent_messages([line for _, line in hits])
            first, last = hits[0][1].timestamp, hits[-1][1].timestamp
            evidence = self._add(
                f"log:{svc}:{name}",
                kind="foreign_log" if foreign else "log_pattern",
                tags=["foreign"] if foreign else ["log", f"log:{name}"],
                summary=f"{prefix}{len(hits)} log line(s) matching '{name}' between {_clock(first)} and {_clock(last)}: {samples[0]}",
                phrase="" if foreign else (f"{samples[0]} (x{len(hits)})" if len(hits) > 1 else samples[0]),
                data={"pattern": name, "count": len(hits), "first": first.isoformat(), "last": last.isoformat(), "samples": samples},
                source=source,
            )
            if evidence:
                created.append(evidence)

        leftovers = [line for i, line in enumerate(lines) if i not in matched and line.level.upper() in {"ERROR", "FATAL"}]
        if leftovers:
            samples = _frequent_messages(leftovers)
            evidence = self._add(
                f"log:{svc}:other_errors",
                kind="foreign_log" if foreign else "log_pattern",
                tags=["foreign"] if foreign else ["log", "log:other_errors"],
                summary=f"{prefix}{len(leftovers)} other error log line(s), e.g. {samples[0]}",
                data={"pattern": "other_errors", "count": len(leftovers), "samples": samples},
                source=source,
            )
            if evidence:
                created.append(evidence)
        return created

    # ----------------------------------------------------------------- deploys

    def add_deploys(
        self, deploys: Iterable[DeployRecord], window_minutes: int, source: str, service: str | None = None
    ) -> list[Evidence]:
        svc = service or self.service
        foreign = svc != self.service
        deploys = list(deploys)
        if not deploys:
            created = self._add(
                f"deploy:{svc}:none",
                kind="foreign_deploy" if foreign else "no_deploy",
                tags=["foreign"] if foreign else ["no_deploy"],
                summary=f"No deploys of {svc} in the last {window_minutes} minutes",
                data={"window_minutes": window_minutes},
                source=source,
            )
            return [created] if created else []

        created_items = []
        for deploy in deploys:
            if foreign:
                created = self._add(
                    f"deploy:{deploy.service}:{deploy.ref}",
                    kind="foreign_deploy",
                    tags=["foreign"],
                    summary=f"[{svc}] {deploy.kind} deploy {deploy.ref} at {_clock(deploy.deployed_at)}",
                    data={"ref": deploy.ref, "kind": deploy.kind, "service": svc},
                    source=source,
                )
                if created:
                    created_items.append(created)
                continue
            created = self._add(
                f"deploy:{deploy.service}:{deploy.ref}",
                kind="deploy",
                tags=["deploy"],
                summary="",
                data={
                    "ref": deploy.ref,
                    "kind": deploy.kind,
                    "deployed_at": deploy.deployed_at.isoformat(),
                    "deployed_by": deploy.deployed_by,
                    "change_summary": clean_text(deploy.summary),
                },
                source=source,
            )
            if created:
                created_items.append(created)
        self._reclassify_deploys()
        return created_items

    def _reclassify_deploys(self) -> None:
        """Relate each deploy to the metric onset. Re-run whenever the onset may have changed."""
        onset = self.onset
        for item in self.items:
            if item.kind != "deploy":
                continue
            deployed_at = datetime.fromisoformat(item.data["deployed_at"])
            kind = item.data["kind"]
            what = f"{kind} deploy {item.data['ref']}"
            note = f" ('{item.data['change_summary']}')" if item.data["change_summary"] else ""
            tags = ["deploy"]
            item.data.pop("lead_minutes", None)

            if onset is None:
                tags.append("deploy_unrelated")
                item.summary = f"{what}{note} at {_clock(deployed_at)}; no metric onset to compare against"
                item.phrase = ""
            elif deployed_at > onset:
                tags.append("deploy_after_onset")
                item.summary = f"{what}{note} at {_clock(deployed_at)}, after the problem had already started"
                item.phrase = ""
            elif onset - deployed_at <= timedelta(minutes=DEPLOY_LEAD_MINUTES):
                lead = round((onset - deployed_at).total_seconds() / 60, 1)
                tags.extend(["deploy_before_onset", f"deploy_before_onset:{kind}"])
                item.data["lead_minutes"] = lead
                item.summary = f"{what}{note} landed {lead} min before the onset"
                item.phrase = f"{what}{note} landed {lead} min before the onset"
            else:
                tags.append("deploy_unrelated")
                item.summary = f"{what}{note} at {_clock(deployed_at)} is too far before the onset to explain it"
                item.phrase = ""
            item.tags = tags

    # ---------------------------------------------------------- similar incidents

    def add_similar(self, incidents: Iterable[ScoredIncident], source: str) -> list[Evidence]:
        created_items = []
        for incident in incidents:
            if incident.score < MIN_SIMILARITY:
                continue
            created = self._add(
                f"similar:{incident.incident_id}",
                kind="similar_incident",
                tags=["similar", f"similar:{incident.issue_type}"],
                summary=(
                    f"Similar past incident {incident.incident_id} ({incident.issue_type}, "
                    f"match {incident.score:.2f}): resolved by '{clean_text(incident.resolution, 160)}'"
                ),
                data={
                    "incident_id": incident.incident_id,
                    "score": incident.score,
                    "issue_type": incident.issue_type,
                    "resolution": clean_text(incident.resolution, 200),
                    "why": incident.evidence,
                },
                source=source,
            )
            if created:
                created_items.append(created)
        return created_items

    def add_error(self, source: str, message: str) -> Evidence | None:
        self.warnings.append(f"{source} failed: {clean_text(message, 160)}")
        return self._add(
            f"error:{source}:{message}",
            kind="tool_error",
            summary=f"{source} failed: {clean_text(message, 160)}",
            source=source,
        )


_SEVERITY = {"FATAL": 3, "ERROR": 2, "WARN": 1, "WARNING": 1}


def _frequent_messages(lines: list[LogLine], limit: int = 3) -> list[str]:
    """Distinct messages, most severe first, then most frequent. Untrusted text is cleaned and capped here."""
    counts: dict[str, int] = {}
    severity: dict[str, int] = {}
    for line in lines:
        message = clean_text(line.message)
        counts[message] = counts.get(message, 0) + 1
        severity[message] = max(severity.get(message, 0), _SEVERITY.get(line.level.upper(), 0))
    ordered = sorted(counts, key=lambda m: (-severity[m], -counts[m], m))
    return ordered[:limit]
