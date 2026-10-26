import math
import re
from collections import Counter
from typing import Callable, Protocol

from app.models.schemas import UNRESOLVED_RESOLUTION, IncidentRecord
from app.tools.models import ScoredIncident

_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were with "
    "high low in prod service issue alert observed detected".split()
)
_SAME_SERVICE_BOOST = 0.25
_MIN_SCORE = 0.08


class IncidentMemory(Protocol):
    def search(
        self, text: str, service: str | None = None, k: int = 3, exclude_ids: list[str] | None = None
    ) -> list[ScoredIncident]:
        """Resolved past incidents most relevant to `text`, best first, each with why it matched.

        `exclude_ids` keeps the incident being investigated out of its own evidence.
        """
        ...


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))
    return [word for word in words if len(word) > 2 and word not in _STOPWORDS]


def _document(record: IncidentRecord) -> str:
    return " ".join(
        [record.service, record.metric, record.issue_type, record.summary, record.resolution, *record.runbook_actions]
    )


class LexicalIncidentMemory:
    """TF-IDF cosine similarity over past incidents, boosted when the service matches.

    A stand-in for embedding search with the same interface: when pgvector and an
    embedder are available they replace this class and nothing that calls it changes.
    Every result lists the terms that matched, so a reader can see why it ranked.
    """

    def __init__(self, records: Callable[[], list[IncidentRecord]]) -> None:
        self._records = records

    def search(
        self, text: str, service: str | None = None, k: int = 3, exclude_ids: list[str] | None = None
    ) -> list[ScoredIncident]:
        excluded = set(exclude_ids or [])
        # Only incidents someone actually resolved can say anything about a resolution; the pipeline files
        # every new incident with a placeholder, and those labels came from the keyword classifier.
        records = [r for r in self._records() if r.incident_id not in excluded and r.resolution.strip() != UNRESOLVED_RESOLUTION]
        query_terms = Counter(_tokens(text))
        if not records or not query_terms:
            return []

        documents = [Counter(_tokens(_document(record))) for record in records]
        doc_count = len(records)
        doc_freq: Counter[str] = Counter()
        for doc in documents:
            doc_freq.update(doc.keys())

        def weight(term: str, count: int) -> float:
            idf = math.log((1 + doc_count) / (1 + doc_freq.get(term, 0))) + 1.0
            return (1 + math.log(count)) * idf

        query_vector = {term: weight(term, count) for term, count in query_terms.items()}
        query_norm = math.sqrt(sum(value * value for value in query_vector.values()))

        scored: list[ScoredIncident] = []
        for record, doc in zip(records, documents):
            doc_vector = {term: weight(term, count) for term, count in doc.items()}
            doc_norm = math.sqrt(sum(value * value for value in doc_vector.values())) or 1.0
            shared = [term for term in query_vector if term in doc_vector]
            dot = sum(query_vector[term] * doc_vector[term] for term in shared)
            similarity = dot / (query_norm * doc_norm) if query_norm else 0.0

            evidence = sorted(shared, key=lambda term: -(query_vector[term] * doc_vector[term]))[:5]
            same_service = service is not None and record.service == service
            score = similarity + (_SAME_SERVICE_BOOST if same_service else 0.0)
            if similarity < _MIN_SCORE and not (same_service and shared):
                continue

            reasons = [f"matched terms: {', '.join(evidence)}"] if evidence else []
            if same_service:
                reasons.append(f"same service ({record.service})")
            scored.append(
                ScoredIncident(
                    incident_id=record.incident_id,
                    score=round(min(1.0, score), 3),
                    service=record.service,
                    metric=record.metric,
                    issue_type=record.issue_type,
                    summary=record.summary,
                    resolution=record.resolution,
                    runbook_actions=list(record.runbook_actions),
                    evidence=reasons,
                    created_at=record.created_at,
                )
            )

        scored.sort(key=lambda item: (-item.score, item.incident_id))
        return scored[: max(1, k)]
