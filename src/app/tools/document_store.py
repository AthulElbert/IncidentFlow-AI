"""Where incident-memory documents and their vectors live.

`PostgresDocumentStore` uses pgvector (an HNSW index for cosine distance) and Postgres full-text search (a GIN index
on a generated `tsvector`). `InMemoryDocumentStore` does the same in Python, for tests and the retrieval eval.

Rows are keyed by document and embedding model, so changing the model never mixes vectors from two models; the
table name includes the dimensions because a pgvector column has a fixed size.
"""
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+")


def Vector(values: list[float]):  # noqa: N802 - pgvector's own type, imported only when Postgres is used
    from pgvector import Vector as PgVector

    return PgVector(values)


@dataclass(frozen=True)
class MemoryDocument:
    doc_id: str
    incident_id: str
    kind: str  # resolution | postmortem
    service: str
    issue_type: str
    metric: str
    summary: str
    resolution: str
    text: str
    created_at: datetime
    runbook_actions: tuple[str, ...] = field(default_factory=tuple)


class DocumentStore(Protocol):
    def upsert_incident(self, incident_id: str, documents: list[MemoryDocument], vectors: list[list[float]], model: str) -> None:
        """Replace every document of one incident (for this model)."""
        ...

    def indexed_incidents(self, model: str) -> set[str]:
        ...

    def vector_search(self, vector: list[float], model: str, k: int, issue_type: str | None = None) -> list[tuple[MemoryDocument, float]]:
        """Nearest documents by cosine similarity, best first."""
        ...

    def text_search(self, text: str, model: str, k: int, issue_type: str | None = None) -> list[MemoryDocument]:
        """Documents sharing words with the text, best first."""
        ...

    def similarities(self, doc_ids: list[str], vector: list[float], model: str) -> dict[str, float]:
        ...


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) > 2]


def _cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


class InMemoryDocumentStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[MemoryDocument, list[float]]] = {}

    def upsert_incident(self, incident_id, documents, vectors, model):
        for key in [key for key, (doc, _) in self._rows.items() if doc.incident_id == incident_id and key[1] == model]:
            del self._rows[key]
        for doc, vector in zip(documents, vectors):
            self._rows[(doc.doc_id, model)] = (doc, list(vector))

    def indexed_incidents(self, model):
        return {doc.incident_id for (_, m), (doc, _) in self._rows.items() if m == model}

    def _candidates(self, model, issue_type):
        return [(doc, vec) for (_, m), (doc, vec) in self._rows.items() if m == model and (issue_type is None or doc.issue_type == issue_type)]

    def vector_search(self, vector, model, k, issue_type=None):
        scored = [(doc, _cosine(vector, vec)) for doc, vec in self._candidates(model, issue_type)]
        return sorted(scored, key=lambda item: (-item[1], item[0].doc_id))[:k]

    def text_search(self, text, model, k, issue_type=None):
        query = set(_words(text))
        scored = [(len(query & set(_words(doc.text))), doc) for doc, _ in self._candidates(model, issue_type)]
        return [doc for hits, doc in sorted(scored, key=lambda item: (-item[0], item[1].doc_id)) if hits][:k]

    def similarities(self, doc_ids, vector, model):
        wanted = set(doc_ids)
        return {doc.doc_id: _cosine(vector, vec) for doc, vec in self._candidates(model, None) if doc.doc_id in wanted}


class PostgresDocumentStore:
    def __init__(self, database_url: str, dimensions: int) -> None:
        if not 1 <= dimensions <= 2000:
            raise ValueError("pgvector's HNSW index takes at most 2000 dimensions")
        self.database_url = database_url
        self.dimensions = dimensions
        self.table = f"incident_documents_{dimensions}"
        self._ready = False

    def _connect(self):
        import psycopg
        from pgvector.psycopg import register_vector

        conn = psycopg.connect(self.database_url)
        if not self._ready:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
        register_vector(conn)
        if not self._ready:
            self._create(conn)
            self._ready = True
        return conn

    def _create(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    doc_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    service TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    runbook JSONB NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
                    embedding vector({self.dimensions}) NOT NULL,
                    PRIMARY KEY (doc_id, model)
                )
                """
            )
            cur.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_embedding ON {self.table} USING hnsw (embedding vector_cosine_ops)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_tsv ON {self.table} USING gin (tsv)")
            cur.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_incident ON {self.table} (incident_id, model)")
        conn.commit()

    _COLUMNS = "doc_id, incident_id, kind, service, issue_type, metric, summary, resolution, text, created_at, runbook"

    @staticmethod
    def _document(row) -> MemoryDocument:
        doc_id, incident_id, kind, service, issue_type, metric, summary, resolution, text, created_at, runbook = row[:11]
        return MemoryDocument(doc_id, incident_id, kind, service, issue_type, metric, summary, resolution, text, created_at,
                              tuple(runbook if isinstance(runbook, list) else json.loads(runbook or "[]")))

    def upsert_incident(self, incident_id, documents, vectors, model):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.table} WHERE incident_id = %s AND model = %s", (incident_id, model))
            for doc, vector in zip(documents, vectors):
                cur.execute(
                    f"INSERT INTO {self.table} ({self._COLUMNS}, model, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (doc.doc_id, doc.incident_id, doc.kind, doc.service, doc.issue_type, doc.metric, doc.summary,
                     doc.resolution, doc.text, doc.created_at, json.dumps(list(doc.runbook_actions)), model, Vector(list(vector))),
                )

    def indexed_incidents(self, model):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT incident_id FROM {self.table} WHERE model = %s", (model,))
            return {row[0] for row in cur.fetchall()}

    def vector_search(self, vector, model, k, issue_type=None):
        query = Vector(list(vector))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLUMNS}, 1 - (embedding <=> %s) FROM {self.table} "
                "WHERE model = %s AND (%s::text IS NULL OR issue_type = %s) ORDER BY embedding <=> %s, doc_id LIMIT %s",
                (query, model, issue_type, issue_type, query, k),
            )
            return [(self._document(row), float(row[11])) for row in cur.fetchall()]

    def text_search(self, text, model, k, issue_type=None):
        words = sorted(set(_words(text)))
        if not words:
            return []
        tsquery = " | ".join(words)  # any word may match; the words are [a-z0-9] only, so this is safe to build
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLUMNS} FROM {self.table}, to_tsquery('english', %s) q "
                "WHERE model = %s AND tsv @@ q AND (%s::text IS NULL OR issue_type = %s) ORDER BY ts_rank_cd(tsv, q) DESC, doc_id LIMIT %s",
                (tsquery, model, issue_type, issue_type, k),
            )
            return [self._document(row) for row in cur.fetchall()]

    def similarities(self, doc_ids, vector, model):
        if not doc_ids:
            return {}
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT doc_id, 1 - (embedding <=> %s) FROM {self.table} WHERE model = %s AND doc_id = ANY(%s)",
                (Vector(list(vector)), model, list(doc_ids)),
            )
            return {row[0]: float(row[1]) for row in cur.fetchall()}
