"""What goes into hybrid incident memory, and when.

Only incidents someone actually resolved (`IncidentRecord.reusable`: historical seed data, or `resolved` by a
released fix) are indexed, so memory never learns from an open incident's placeholder or a rejected fix.

Each incident becomes one `resolution` document, plus one document per section of its finalised postmortem if a
person has finalised one: a match then points at the relevant part (the diagnosis, the change, the lessons), not
at a whole page. Postmortem drafts are never indexed; they are machine-written until a person signs them off.
"""
import logging
import re

from app.models.schemas import IncidentRecord
from app.tools.document_store import DocumentStore, MemoryDocument
from app.tools.embeddings import Embedder

logger = logging.getLogger("app.memory_indexer")

MAX_SECTION_CHARS = 2000
# Postmortem sections worth retrieving. The timeline and decisions are about this run, not about the next incident.
INDEXED_SECTIONS = ("diagnosis", "what was changed", "how it was checked", "for the people involved to fill in", "lessons")


def documents_for(incident: IncidentRecord, postmortem: str | None = None) -> list[MemoryDocument]:
    def doc(doc_id: str, kind: str, text: str) -> MemoryDocument:
        return MemoryDocument(
            doc_id=doc_id, incident_id=incident.incident_id, kind=kind, service=incident.service,
            issue_type=incident.issue_type, metric=incident.metric, summary=incident.summary,
            resolution=incident.resolution, text=text[:MAX_SECTION_CHARS], created_at=incident.created_at,
            runbook_actions=tuple(incident.runbook_actions),
        )

    head = f"{incident.service} {incident.metric} ({incident.issue_type}): {incident.summary}"
    docs = [doc(f"{incident.incident_id}:resolution", "resolution",
                f"{head}. Resolved by: {incident.resolution}. Actions: {'; '.join(incident.runbook_actions)}")]
    for title, body in _sections(postmortem or ""):
        if title.lower().strip() in INDEXED_SECTIONS and body.strip():
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            docs.append(doc(f"{incident.incident_id}:postmortem:{slug}", "postmortem", f"{head}. {title}: {body.strip()}"))
    return docs


def _sections(markdown: str) -> list[tuple[str, str]]:
    sections, title, lines = [], None, []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if title is not None:
                sections.append((title, "\n".join(lines)))
            title, lines = line[3:].strip(), []
        elif title is not None:
            lines.append(line)
    if title is not None:
        sections.append((title, "\n".join(lines)))
    return sections


class MemoryIndexer:
    def __init__(self, store: DocumentStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    def index(self, incident: IncidentRecord, postmortem: str | None = None) -> int:
        """(Re)index one incident. Returns how many documents were written; 0 if it may not be remembered."""
        if not incident.reusable:
            return 0
        docs = documents_for(incident, postmortem)
        vectors = self.embedder.embed([d.text for d in docs])
        self.store.upsert_incident(incident.incident_id, docs, vectors, self.embedder.model)
        return len(docs)

    def sync(self, incidents: list[IncidentRecord], postmortems: dict[str, str] | None = None) -> int:
        """Index every reusable incident not yet indexed for the current embedding model. Returns how many."""
        done = self.store.indexed_incidents(self.embedder.model)
        added = 0
        for incident in incidents:
            if incident.reusable and incident.incident_id not in done:
                self.index(incident, (postmortems or {}).get(incident.incident_id))
                added += 1
        return added


def finalized_postmortems(changes) -> dict[str, str]:
    """incident id -> the latest finalised postmortem among its changes."""
    latest: dict[str, tuple[str, str]] = {}
    for record in changes:
        pm = record.postmortem or {}
        if pm.get("markdown"):
            stamp = str(pm.get("finalized_at", ""))
            if record.incident_id not in latest or stamp > latest[record.incident_id][0]:
                latest[record.incident_id] = (stamp, pm["markdown"])
    return {incident_id: markdown for incident_id, (_, markdown) in latest.items()}


class MemorySync:
    """Keeps hybrid memory in step with the knowledge base and finalised postmortems.

    Failures are logged and never raised: memory that lags behind is better than an outcome or a postmortem that
    cannot be recorded because the embedding API is down. The startup sync catches up incidents never indexed.
    """

    def __init__(self, indexer: MemoryIndexer, kb, change_store) -> None:
        self.indexer = indexer
        self.kb = kb
        self.change_store = change_store

    def incident(self, incident_id: str) -> int:
        try:
            incident = self.kb.get(incident_id)
            if incident is None:
                return 0
            postmortem = finalized_postmortems(
                r for r in self.change_store.list_changes() if r.incident_id == incident_id
            ).get(incident_id)
            written = self.indexer.index(incident, postmortem)
            logger.info("memory-indexed", extra={"incident_id": incident_id, "documents": written})
            return written
        except Exception as exc:
            logger.warning("memory-index-failed", extra={"incident_id": incident_id, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            return 0

    def sync_all(self) -> int:
        try:
            added = self.indexer.sync(self.kb.all_records(), finalized_postmortems(self.change_store.list_changes()))
            if added:
                logger.info("memory-synced", extra={"incidents": added})
            return added
        except Exception as exc:
            logger.warning("memory-sync-failed", extra={"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            return 0
