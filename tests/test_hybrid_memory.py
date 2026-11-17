"""Hybrid incident memory: embeddings, the document store, fused search with a similarity floor, and indexing."""
import io
import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest
from agent_harness import APPROVER, RELEASE, VIEWER
from test_learning import _incident_of, _verified

from app.adapters.llm_client import LLMError
from app.config import ConfigError, load_settings, validate_settings
from app.memory_eval import CASES, CachingEmbedder, corpus, evaluate
from app.models.schemas import IncidentRecord
from app.services.memory_indexer import MemoryIndexer, MemorySync, documents_for, finalized_postmortems
from app.tools.base import ToolError
from app.tools.document_store import InMemoryDocumentStore, PostgresDocumentStore
from app.tools.embeddings import HashingEmbedder, OpenAICompatibleEmbedder
from app.tools.hybrid_memory import HybridIncidentMemory, rescale
from app.tools.memory import LexicalIncidentMemory
from app.tools.registry import REGISTRY, ToolRequestError, validate_request

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _incident(incident_id="INC-1", outcome="historical", **fields):
    values = {"service": "faulty-project", "metric": "latency_p95_seconds", "issue_type": "performance_degradation",
              "summary": "config deploy raised latency", "resolution": "Rolled back the bad config deploy.", **fields}
    return IncidentRecord(incident_id=incident_id, created_at=NOW, outcome=outcome, **values)

POSTMORTEM = """# Postmortem: CHG-1

## Summary
Latency went up.

## Diagnosis
A config deploy lowered the database connection pool to 2, so requests queued for a connection.

## Timeline
- 10:00 alert

## Lessons
Review pool sizes in config changes.
"""


class TopicEmbedder:
    """A fake embedder with *meaning*: each text maps to the topics it mentions, so paraphrases land close."""

    TOPICS = {
        "pool": ("pool", "connection", "connections", "saturated", "exhausted"),
        "memory": ("oom", "ram", "heap", "memory", "killed"),
        "deploy": ("deploy", "release", "rolled", "rollback"),
        "tls": ("tls", "certificate", "expired"),
    }
    model = "topics-5"
    dimensions = 5

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            words = text.lower().replace(",", " ").replace(".", " ").split()
            vector = [float(sum(w in words for w in topic)) for topic in self.TOPICS.values()] + [0.3]
            out.append(vector)
        return out


def _memory(records, embedder=None, floor=0.5):
    embedder = embedder or TopicEmbedder()
    store = InMemoryDocumentStore()
    MemoryIndexer(store, embedder).sync(records)
    return HybridIncidentMemory(store, embedder, floor=floor), store


# --- the similarity floor ---------------------------------------------------------------------------------------


def test_the_floor_rescales_similarity_so_unrelated_text_scores_zero():
    assert rescale(0.45, 0.5) == 0.0
    assert rescale(0.5, 0.5) == 0.0
    assert rescale(0.75, 0.5) == pytest.approx(0.5)
    assert rescale(1.0, 0.5) == pytest.approx(1.0)
    assert rescale(0.9, 0.0) == pytest.approx(0.9)


# --- embedders ---------------------------------------------------------------------------------------------------


def test_the_hashing_embedder_is_deterministic_normalised_and_offline():
    embedder = HashingEmbedder(64)
    first, again, other = embedder.embed(["pool exhausted", "pool exhausted", "certificate expired"])
    assert first == again and first != other
    assert len(first) == 64 and sum(v * v for v in first) == pytest.approx(1.0)
    assert embedder.model == "hashing-64"


class _Reply:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _vectors(n, dims):
    return {"data": [{"index": i, "embedding": [0.1] * dims} for i in reversed(range(n))]}


def test_the_api_embedder_asks_for_fixed_dimensions_in_batches(monkeypatch):
    sent = []

    def urlopen(req, timeout):
        body = json.loads(req.data)
        sent.append(body)
        assert req.full_url == "https://llm.example/v1/embeddings" and req.headers["Authorization"] == "Bearer k"
        return _Reply(_vectors(len(body["input"]), 8))

    monkeypatch.setattr("app.tools.embeddings.request.urlopen", urlopen)
    vectors = OpenAICompatibleEmbedder("https://llm.example/v1/", "k", "embed-1", dimensions=8).embed([f"t{i}" for i in range(70)])

    assert len(vectors) == 70 and all(len(v) == 8 for v in vectors)
    assert [len(b["input"]) for b in sent] == [64, 6]
    assert all(b["dimensions"] == 8 and b["model"] == "embed-1" for b in sent)


def test_the_api_embedder_retries_a_rate_limit_as_long_as_asked_and_refuses_a_wrong_reply(monkeypatch):
    waits, replies = [], iter(["429", _vectors(1, 4), {"data": [{"embedding": [0.1, 0.2]}]}])

    def urlopen(req, timeout):
        reply = next(replies)
        if reply == "429":
            raise HTTPError(req.full_url, 429, "busy", {"Retry-After": "3"}, io.BytesIO(b"slow down"))
        return _Reply(reply)

    monkeypatch.setattr("app.tools.embeddings.request.urlopen", urlopen)
    embedder = OpenAICompatibleEmbedder("https://llm.example/v1", "k", "embed-1", dimensions=4, sleep=waits.append)
    assert len(embedder.embed(["one"])) == 1 and waits == [3.0]
    with pytest.raises(LLMError, match="wrong size"):
        embedder.embed(["two"])


def test_the_api_embedder_does_not_wait_out_a_daily_quota(monkeypatch):
    def urlopen(req, timeout):
        raise HTTPError(req.full_url, 429, "quota", {"Retry-After": "3600"}, io.BytesIO(b"daily quota"))

    monkeypatch.setattr("app.tools.embeddings.request.urlopen", urlopen)
    waits = []
    with pytest.raises(LLMError):
        OpenAICompatibleEmbedder("https://llm.example/v1", "k", "embed-1", sleep=waits.append).embed(["x"])
    assert waits == []


# --- what is indexed ---------------------------------------------------------------------------------------------


def test_only_resolved_incidents_are_indexed_and_only_the_useful_postmortem_sections():
    store, embedder = InMemoryDocumentStore(), HashingEmbedder(32)
    indexer = MemoryIndexer(store, embedder)
    assert indexer.index(_incident("INC-OPEN", outcome="open")) == 0
    assert indexer.index(_incident("INC-REJ", outcome="rejected")) == 0

    docs = documents_for(_incident("INC-1"), POSTMORTEM)
    assert [d.doc_id for d in docs] == ["INC-1:resolution", "INC-1:postmortem:diagnosis", "INC-1:postmortem:lessons"]
    assert "connection pool" in docs[1].text and docs[1].kind == "postmortem"

    assert indexer.index(_incident("INC-1"), POSTMORTEM) == 3
    assert indexer.index(_incident("INC-1")) == 1  # re-indexing replaces the incident's documents
    assert store.indexed_incidents(embedder.model) == {"INC-1"}
    assert store.indexed_incidents("another-model") == set()


def test_sync_indexes_only_what_the_current_model_has_not_seen():
    store, embedder = InMemoryDocumentStore(), TopicEmbedder()
    indexer = MemoryIndexer(store, embedder)
    records = [_incident("INC-1"), _incident("INC-2"), _incident("INC-3", outcome="open")]
    assert indexer.sync(records) == 2
    calls = embedder.calls
    assert indexer.sync(records) == 0 and embedder.calls == calls


def test_the_latest_finalised_postmortem_of_an_incident_wins():
    class Change:
        def __init__(self, incident_id, postmortem):
            self.incident_id, self.postmortem = incident_id, postmortem

    changes = [
        Change("INC-1", {"markdown": "old", "finalized_at": "2026-07-01T10:00:00"}),
        Change("INC-1", {"markdown": "new", "finalized_at": "2026-07-02T10:00:00"}),
        Change("INC-2", {}),
    ]
    assert finalized_postmortems(changes) == {"INC-1": "new"}


# --- search -------------------------------------------------------------------------------------------------------


def _records():
    return [
        _incident("INC-POOL", summary="checkout timeouts", resolution="Raised the connection pool size back to 20.",
                  service="checkout", issue_type="dependency_failure"),
        _incident("INC-OOM", summary="worker restarts", resolution="Raised the heap limit after OOM kills.",
                  service="worker", issue_type="resource_saturation"),
        _incident("INC-DEPLOY", summary="errors after release", resolution="Rolled back the release.",
                  service="orders", issue_type="application_error"),
    ]


def test_search_finds_a_past_incident_described_in_other_words():
    memory, _ = _memory(_records())
    results = memory.search("database connections saturated", k=3)
    assert results[0].incident_id == "INC-POOL" and results[0].score >= 0.30
    assert "similar in meaning" in results[0].evidence[0]


def test_shared_words_alone_do_not_make_an_incident_similar():
    memory, _ = _memory(_records())
    # "raised" and "size" are in INC-POOL's text, but nothing here is about the same thing
    assert memory.search("the TLS certificate expired, raised a ticket about its size") == []


def test_search_excludes_the_incident_itself_filters_by_issue_type_and_boosts_the_same_service():
    memory, _ = _memory(_records())
    assert [r.incident_id for r in memory.search("connection pool exhausted", exclude_ids=["INC-POOL"])] == []
    assert memory.search("connection pool exhausted", issue_type="resource_saturation") == []
    plain = memory.search("connection pool exhausted")[0]
    boosted = memory.search("connection pool exhausted", service="checkout")[0]
    assert boosted.score == pytest.approx(min(1.0, plain.score + 0.10))
    assert "same service (checkout)" in boosted.evidence


def test_a_match_from_a_finalised_postmortem_says_so():
    store, embedder = InMemoryDocumentStore(), TopicEmbedder()
    MemoryIndexer(store, embedder).index(
        _incident("INC-PM", summary="slow", resolution="Fixed the config.", issue_type="performance_degradation"), POSTMORTEM
    )
    result = HybridIncidentMemory(store, embedder).search("requests queued for a database connection")[0]
    assert result.incident_id == "INC-PM" and "from its finalised postmortem" in result.evidence


def test_a_failing_embedding_api_is_a_tool_error_the_agents_already_handle():
    class Down:
        model, dimensions = "down", 4

        def embed(self, texts):
            raise LLMError("embeddings API error 503: overloaded", 503, True)

    with pytest.raises(ToolError, match="incident memory unavailable"):
        HybridIncidentMemory(InMemoryDocumentStore(), Down()).search("anything")


def test_the_lexical_memory_takes_the_same_issue_type_filter():
    memory = LexicalIncidentMemory(lambda: _records())
    assert memory.search("connection pool size", issue_type="dependency_failure")[0].incident_id == "INC-POOL"
    assert memory.search("connection pool size", issue_type="application_error") == []


def test_a_model_may_narrow_memory_by_a_known_issue_type_only():
    spec, args = validate_request("investigator", {"tool": "memory", "method": "search", "args": {"text": "pool", "issue_type": "dependency_failure"}})
    assert args == {"text": "pool", "issue_type": "dependency_failure"}
    with pytest.raises(ToolRequestError, match="must be one of"):
        validate_request("fixer", {"tool": "memory", "method": "search", "args": {"text": "pool", "issue_type": "anything"}})


def test_the_registry_issue_types_match_the_agents_issue_types():
    from app.agents.models import ISSUE_TYPES

    arg = next(a for s in REGISTRY if s.id == "memory.search" for a in s.args if a.name == "issue_type")
    assert set(arg.choices) == set(ISSUE_TYPES) - {"unknown"}


# --- the retrieval check -----------------------------------------------------------------------------------------


def test_the_retrieval_check_runs_offline_and_counts_false_matches():
    records = corpus()
    assert len(records) == 7 and sum(1 for c in CASES if not c.expected) == 4
    result = evaluate(LexicalIncidentMemory(lambda: records))
    assert 0.0 <= result.found_rate <= 1.0 and len(result.rows) == len(CASES)

    embedder = CachingEmbedder(HashingEmbedder())
    store = InMemoryDocumentStore()
    MemoryIndexer(store, embedder).sync(records)
    # The hashing embedder captures no meaning: a high floor must leave it matching nothing rather than everything.
    assert evaluate(HybridIncidentMemory(store, embedder, floor=0.5)).false_matches == 0


# --- settings ------------------------------------------------------------------------------------------------------


def test_hybrid_memory_needs_a_database_and_an_embedding_key(monkeypatch):
    monkeypatch.setenv("MEMORY_MODE", "hybrid")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        validate_settings(load_settings())
    assert "requires DATABASE_URL" in str(exc.value) and "requires LLM_API_KEY" in str(exc.value)

    monkeypatch.setenv("EMBEDDING_MODE", "hashing")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "4096")
    monkeypatch.setenv("MEMORY_SEMANTIC_FLOOR", "1.0")
    with pytest.raises(ConfigError) as exc:
        validate_settings(load_settings())
    assert "EMBEDDING_DIMENSIONS" in str(exc.value) and "MEMORY_SEMANTIC_FLOOR" in str(exc.value)


# --- through the app ------------------------------------------------------------------------------------------------


def _hybrid(main_module):
    """Swap the app's memory for hybrid memory on an in-memory store, as MEMORY_MODE=hybrid would wire it."""
    store, embedder = InMemoryDocumentStore(), TopicEmbedder()
    indexer = MemoryIndexer(store, embedder)
    main_module.memory_sync = MemorySync(indexer, main_module.pipeline.kb, main_module.change_store)
    main_module.outcome_learner.on_learned = main_module.memory_sync.incident
    main_module.memory_sync.sync_all()
    return store, embedder


def test_a_released_fix_and_its_finalised_postmortem_become_hybrid_memory(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    store, embedder = _hybrid(main_module)
    change_id = _verified(main_module, client, tmp_path)
    incident_id = _incident_of(main_module, client, change_id).incident_id
    assert incident_id not in store.indexed_incidents(embedder.model)

    # Not closed yet: nothing to finalise
    early = client.put(f"/v1/changes/{change_id}/postmortem", headers=APPROVER, json={"markdown": POSTMORTEM})
    assert early.status_code == 409

    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})
    main_module.outcome_learner.run_once()
    assert incident_id in store.indexed_incidents(embedder.model)

    assert client.put(f"/v1/changes/{change_id}/postmortem", headers=VIEWER, json={"markdown": POSTMORTEM}).status_code == 403
    done = client.put(f"/v1/changes/{change_id}/postmortem", headers=APPROVER, json={"markdown": POSTMORTEM})
    assert done.status_code == 200 and done.json()["revision"] == 1 and done.json()["memory_documents"] == 3

    body = client.get(f"/v1/changes/{change_id}/postmortem", headers=VIEWER).json()
    assert body["finalized"] is True and body["markdown"] == POSTMORTEM and body["finalized_by"] == "approver-user"
    actions = [item["action"] for item in client.get(f"/v1/audit?entity_id={change_id}", headers=APPROVER).json()]
    assert "postmortem.finalized" in actions

    again = client.put(f"/v1/changes/{change_id}/postmortem", headers=APPROVER, json={"markdown": POSTMORTEM + "\nmore"})
    assert again.json()["revision"] == 2
    assert client.put("/v1/changes/CHG-NOPE/postmortem", headers=APPROVER, json={"markdown": POSTMORTEM}).status_code == 404


def test_an_embedding_outage_never_blocks_an_outcome_or_a_postmortem(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    _hybrid(main_module)

    class Down:
        model, dimensions = "down", 4

        def embed(self, texts):
            raise LLMError("embeddings API error 503", 503, True)

    main_module.memory_sync.indexer.embedder = Down()
    change_id = _verified(main_module, client, tmp_path)
    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})
    main_module.outcome_learner.run_once()
    assert _incident_of(main_module, client, change_id).outcome == "resolved"
    done = client.put(f"/v1/changes/{change_id}/postmortem", headers=APPROVER, json={"markdown": POSTMORTEM})
    assert done.status_code == 200 and done.json()["memory_documents"] == 0


# --- a real Postgres with pgvector (runs when TEST_PGVECTOR_URL is set) --------------------------------------------

PGVECTOR_URL = os.getenv("TEST_PGVECTOR_URL")
needs_pgvector = pytest.mark.skipif(not PGVECTOR_URL, reason="TEST_PGVECTOR_URL not set")


def _fresh_store(dimensions):
    import psycopg

    with psycopg.connect(PGVECTOR_URL) as conn:
        conn.execute(f"DROP TABLE IF EXISTS incident_documents_{dimensions}")
    return PostgresDocumentStore(PGVECTOR_URL, dimensions)


@needs_pgvector
def test_the_postgres_store_matches_the_in_memory_store():
    embedder = TopicEmbedder()
    pg, local = _fresh_store(embedder.dimensions), InMemoryDocumentStore()
    for store in (pg, local):
        indexer = MemoryIndexer(store, embedder)
        indexer.sync(_records())
        indexer.index(_records()[0], POSTMORTEM)

    assert pg.indexed_incidents(embedder.model) == local.indexed_incidents(embedder.model) == {"INC-POOL", "INC-OOM", "INC-DEPLOY"}
    query = embedder.embed(["connection pool saturated"])[0]
    pg_hits, local_hits = pg.vector_search(query, embedder.model, 3), local.vector_search(query, embedder.model, 3)
    assert [d.doc_id for d, _ in pg_hits] == [d.doc_id for d, _ in local_hits]
    assert [round(c, 4) for _, c in pg_hits] == [round(c, 4) for _, c in local_hits]
    assert pg_hits[0][0].runbook_actions == local_hits[0][0].runbook_actions
    assert {d.incident_id for d in pg.text_search("rolled back the release", embedder.model, 5)} >= {"INC-DEPLOY"}
    assert pg.text_search("connection pool", embedder.model, 5, issue_type="application_error") == []
    assert pg.vector_search(query, "another-model", 3) == []
    ids = [d.doc_id for d, _ in pg_hits]
    assert pg.similarities(ids, query, embedder.model).keys() == set(ids)

    pg_memory = HybridIncidentMemory(pg, embedder).search("database connections saturated", service="checkout")
    local_memory = HybridIncidentMemory(local, embedder).search("database connections saturated", service="checkout")
    assert [(r.incident_id, r.score, r.evidence) for r in pg_memory] == [(r.incident_id, r.score, r.evidence) for r in local_memory]


@needs_pgvector
def test_the_app_starts_with_hybrid_memory_on_pgvector_and_indexes_the_seed_incidents(make_app, tmp_path):
    _fresh_store(48)
    history = [r.model_dump(mode="json") for r in _records() + [_incident("INC-OPEN", outcome="open")]]
    (tmp_path / "data" / "incident_history.json").write_text(json.dumps(history), encoding="utf-8")
    main_module, client = make_app(MEMORY_MODE="hybrid", EMBEDDING_MODE="hashing", EMBEDDING_DIMENSIONS="48", DATABASE_URL=PGVECTOR_URL)
    assert main_module.tool_modes["memory"] == "hybrid:hashing-48"
    store = main_module.memory_sync.indexer.store
    seeds = {r.incident_id for r in main_module.pipeline.kb.all_records() if r.reusable}
    assert seeds == {"INC-POOL", "INC-OOM", "INC-DEPLOY"} and store.indexed_incidents("hashing-48") == seeds
    results = main_module.toolkit.memory.search("Rolled back the release", k=3)
    assert results and results[0].incident_id == "INC-DEPLOY"  # the hashing embedder matches shared words only
    assert isinstance(main_module.toolkit.memory, HybridIncidentMemory)
