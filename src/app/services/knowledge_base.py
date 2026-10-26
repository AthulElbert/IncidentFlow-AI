import json
from pathlib import Path

from app.models.schemas import UNRESOLVED_RESOLUTION, IncidentRecord, SimilarIncident
from app.storage import atomic_write_text


class IncidentKnowledgeBase:
    def __init__(self, data_file: str, storage_backend: str = "json", database_url: str = "") -> None:
        self.data_path = Path(data_file)
        self.storage_backend = storage_backend.strip().lower()
        self.database_url = database_url.strip()
        self._use_postgres = self.storage_backend == "postgres"
        self._records: list[IncidentRecord] = self._load()

    def _load(self) -> list[IncidentRecord]:
        if self._use_postgres:
            return self._load_from_postgres()

        if not self.data_path.exists():
            return []
        # Accept UTF-8 files with or without BOM.
        raw = json.loads(self.data_path.read_text(encoding="utf-8-sig"))
        return [IncidentRecord(**item) for item in raw]

    def all_records(self) -> list[IncidentRecord]:
        return list(self._records)

    def add_record(self, record: IncidentRecord) -> None:
        self._records.append(record)
        if self._use_postgres:
            self._add_record_postgres(record)
            return

        payload = [item.model_dump(mode="json") for item in self._records]
        atomic_write_text(self.data_path, json.dumps(payload, indent=2))

    def get(self, incident_id: str) -> IncidentRecord | None:
        return next((item for item in self._records if item.incident_id == incident_id), None)

    def update_record(self, record: IncidentRecord) -> None:
        """Replace the stored record with the same id (used when an outcome is learned)."""
        for index, existing in enumerate(self._records):
            if existing.incident_id == record.incident_id:
                self._records[index] = record
                break
        else:
            raise ValueError(f"Unknown incident {record.incident_id}")
        if self._use_postgres:
            self._add_record_postgres(record)
            return
        payload = [item.model_dump(mode="json") for item in self._records]
        atomic_write_text(self.data_path, json.dumps(payload, indent=2))

    def find_similar(self, service: str, metric: str, issue_type: str, top_k: int = 3) -> list[SimilarIncident]:
        scored: list[SimilarIncident] = []
        for rec in self._records:
            score = 0.0
            if rec.service == service:
                score += 0.45
            if rec.metric == metric:
                score += 0.35
            if rec.issue_type == issue_type:
                score += 0.20

            if score > 0:
                scored.append(
                    SimilarIncident(
                        incident_id=rec.incident_id,
                        score=round(score, 2),
                        summary=rec.summary,
                        # An incident nobody has resolved yet must not lend its placeholder (or a copy of someone
                        # else's resolution) to the next one; it still counts as a similar, recurring incident.
                        resolution=rec.resolution if rec.reusable else UNRESOLVED_RESOLUTION,
                    )
                )

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]

    def _load_from_postgres(self) -> list[IncidentRecord]:
        conn = self._pg_connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incidents (
                        incident_id TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute("SELECT payload FROM incidents ORDER BY created_at ASC")
                rows = cur.fetchall()
        conn.close()
        return [IncidentRecord(**row[0]) for row in rows]

    def _add_record_postgres(self, record: IncidentRecord) -> None:
        conn = self._pg_connect()
        payload = json.dumps(record.model_dump(mode="json"))
        created_at = record.created_at.isoformat()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO incidents (incident_id, payload, created_at)
                    VALUES (%s, %s::jsonb, %s)
                    ON CONFLICT (incident_id) DO UPDATE
                    SET payload = EXCLUDED.payload, created_at = EXCLUDED.created_at
                    """,
                    (record.incident_id, payload, created_at),
                )
        conn.close()

    def _pg_connect(self):
        if not self.database_url:
            raise ValueError("STORAGE_BACKEND=postgres requires DATABASE_URL")
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover - import guard
            raise ValueError("Postgres backend requires 'psycopg' package") from exc
        return psycopg.connect(self.database_url)
