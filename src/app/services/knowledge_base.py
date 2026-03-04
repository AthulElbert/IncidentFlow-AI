import json
from pathlib import Path

from app.models.schemas import IncidentRecord, SimilarIncident


class IncidentKnowledgeBase:
    def __init__(self, data_file: str) -> None:
        self.data_path = Path(data_file)
        self._records: list[IncidentRecord] = self._load()

    def _load(self) -> list[IncidentRecord]:
        if not self.data_path.exists():
            return []
        # Accept UTF-8 files with or without BOM.
        raw = json.loads(self.data_path.read_text(encoding="utf-8-sig"))
        return [IncidentRecord(**item) for item in raw]

    def add_record(self, record: IncidentRecord) -> None:
        self._records.append(record)
        payload = [item.model_dump(mode="json") for item in self._records]
        self.data_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

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
                        resolution=rec.resolution,
                    )
                )

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:top_k]
