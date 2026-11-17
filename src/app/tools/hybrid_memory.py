"""Incident memory that searches by meaning and by words, with the same interface as `LexicalIncidentMemory`.

Two searches find candidates: nearest vectors (meaning) and full-text (shared words). Reciprocal rank fusion orders
them. Then meaning decides whether a candidate counts at all.

Why meaning decides: with a real embedding model (Gemini `gemini-embedding-001`, 768 dimensions) a query about
something no past incident covers still scores a cosine of 0.53-0.59 against the nearest one, a past incident in the
same domain with a different cause 0.56-0.66, and the right incident 0.68-0.81 (`scripts/eval_memory.py`). A raw
cosine would make almost everything "similar", and a similar past incident raises a diagnosis' confidence. So the
score reported to the Investigator is the similarity rescaled above a floor (`MEMORY_SEMANTIC_FLOOR`):
`(cosine - floor) / (1 - floor)`, 0 below it, and the Investigator's existing threshold (0.30) applies unchanged. At
the default floor of 0.5 that threshold is a cosine of 0.65: above every unrelated query in the eval, below every
right match. The margin is small and the eval is small; re-run it on your own incidents before trusting the floor.

Only incidents that someone resolved are indexed (see `services/memory_indexer.py`), so the `reusable` rule of the
lexical memory holds here by construction.
"""
from app.tools.base import ToolError
from app.tools.document_store import DocumentStore, MemoryDocument
from app.tools.embeddings import Embedder
from app.tools.models import ScoredIncident

SAME_SERVICE_BOOST = 0.10
RRF_K = 60
CANDIDATES = 10


def rescale(cosine: float, floor: float) -> float:
    return max(0.0, (cosine - floor) / (1.0 - floor)) if floor < 1.0 else 0.0


class HybridIncidentMemory:
    def __init__(self, store: DocumentStore, embedder: Embedder, floor: float = 0.5, candidates: int = CANDIDATES) -> None:
        self.store = store
        self.embedder = embedder
        self.floor = floor
        self.candidates = candidates

    def search(
        self,
        text: str,
        service: str | None = None,
        k: int = 3,
        exclude_ids: list[str] | None = None,
        issue_type: str | None = None,
    ) -> list[ScoredIncident]:
        if not text.strip():
            return []
        try:
            return self._search(text, service, k, exclude_ids, issue_type)
        except ToolError:
            raise
        except Exception as exc:  # the embedding API or the database: an agent records it as a failed tool call
            raise ToolError(f"incident memory unavailable: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def _search(self, text, service, k, exclude_ids, issue_type) -> list[ScoredIncident]:
        model = self.embedder.model
        query = self.embedder.embed([text])[0]
        by_meaning = self.store.vector_search(query, model, self.candidates, issue_type)
        by_words = self.store.text_search(text, model, self.candidates, issue_type)

        fused: dict[str, float] = {}
        documents: dict[str, MemoryDocument] = {}
        similarity: dict[str, float] = {}
        for rank, (doc, cosine) in enumerate(by_meaning):
            fused[doc.doc_id] = fused.get(doc.doc_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            documents[doc.doc_id], similarity[doc.doc_id] = doc, cosine
        word_hits = set()
        for rank, doc in enumerate(by_words):
            fused[doc.doc_id] = fused.get(doc.doc_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            documents[doc.doc_id] = doc
            word_hits.add(doc.doc_id)
        missing = [doc_id for doc_id in documents if doc_id not in similarity]
        similarity.update(self.store.similarities(missing, query, model))

        # One result per incident: its best document.
        excluded = set(exclude_ids or [])
        best: dict[str, str] = {}
        for doc_id in sorted(documents, key=lambda d: (-fused[d], d)):
            incident = documents[doc_id].incident_id
            if incident not in excluded and incident not in best:
                best[incident] = doc_id

        results: list[tuple[float, ScoredIncident]] = []
        for incident, doc_id in best.items():
            doc, cosine = documents[doc_id], similarity.get(doc_id, 0.0)
            meaning = rescale(cosine, self.floor)
            if meaning <= 0.0:
                continue  # shared words alone do not make a past incident similar
            same_service = service is not None and doc.service == service
            reasons = [f"similar in meaning (cosine {cosine:.2f} above the {self.floor:.2f} floor)"]
            if doc_id in word_hits:
                reasons.append("shares words with this incident")
            if doc.kind == "postmortem":
                reasons.append("from its finalised postmortem")
            if same_service:
                reasons.append(f"same service ({doc.service})")
            score = min(1.0, meaning + (SAME_SERVICE_BOOST if same_service else 0.0))
            results.append(
                (
                    fused[doc_id],
                    ScoredIncident(
                        incident_id=incident, score=round(score, 3), service=doc.service, metric=doc.metric,
                        issue_type=doc.issue_type, summary=doc.summary, resolution=doc.resolution,
                        runbook_actions=list(doc.runbook_actions), evidence=reasons, created_at=doc.created_at,
                    ),
                )
            )
        results.sort(key=lambda item: (-item[0], -item[1].score, item[1].incident_id))
        return [incident for _, incident in results[: max(1, k)]]
