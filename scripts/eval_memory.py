"""Compare incident-memory search modes on the retrieval check, and choose the similarity floor.

    PYTHONPATH=src python scripts/eval_memory.py                      # lexical, and hybrid with the offline hashing embedder
    PYTHONPATH=src python scripts/eval_memory.py --embedder real      # hybrid with EMBEDDING_* settings (calls the API)
    ... --database-url postgresql://...                                 # store the documents in pgvector instead

Uses an in-memory document store unless a database is given (its eval table is dropped and rebuilt). The cases were written with the code they check: a
sanity check and a way to pick the floor, not an accuracy figure.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.config import load_settings  # noqa: E402
from app.memory_eval import CASES, CachingEmbedder, corpus, evaluate  # noqa: E402
from app.services.memory_indexer import MemoryIndexer  # noqa: E402
from app.tools.document_store import InMemoryDocumentStore  # noqa: E402
from app.tools.embeddings import HashingEmbedder, OpenAICompatibleEmbedder  # noqa: E402
from app.tools.hybrid_memory import HybridIncidentMemory  # noqa: E402
from app.tools.memory import LexicalIncidentMemory  # noqa: E402

FLOORS = (0.0, 0.3, 0.5, 0.55, 0.6, 0.62, 0.65, 0.68, 0.7)


def line(label, result):
    print(f"{label:34} found {result.found_rate:>5.0%}  MRR {result.mean_reciprocal_rank:.2f}  "
          f"wrong top {result.wrong_top}  false matches {result.false_matches}/{sum(1 for c in CASES if not c.expected)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--embedder", choices=["hashing", "real"], default="hashing")
    parser.add_argument("--show", action="store_true", help="print what each query matched")
    parser.add_argument("--database-url", default="", help="a Postgres database with pgvector (a throwaway one)")
    args = parser.parse_args()

    records = corpus()
    line("lexical (TF-IDF)", evaluate(LexicalIncidentMemory(lambda: records)))

    if args.embedder == "real":
        settings = load_settings(str(ROOT))
        key = os.environ.get(settings.embedding_api_key_env, "")
        inner = OpenAICompatibleEmbedder(settings.embedding_base_url, key, settings.embedding_model, settings.embedding_dimensions)
    else:
        inner = HashingEmbedder()
    embedder = CachingEmbedder(inner)
    if args.database_url:
        import psycopg

        from app.tools.document_store import PostgresDocumentStore

        store = PostgresDocumentStore(args.database_url, inner.dimensions)
        with psycopg.connect(args.database_url) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {store.table}")
    else:
        store = InMemoryDocumentStore()
    MemoryIndexer(store, embedder).sync(records)
    embedder.embed([case.query for case in CASES])  # one batch for every query

    best = None
    for floor in FLOORS:
        result = evaluate(HybridIncidentMemory(store, embedder, floor=floor))
        line(f"hybrid {embedder.model} floor {floor:.2f}", result)
        if best is None or (result.false_matches, -result.found_rate, result.wrong_top) < (best[1].false_matches, -best[1].found_rate, best[1].wrong_top):
            best = (floor, result)
    print(f"\nbest floor here: {best[0]:.2f} (fewest false matches, then most found, then fewest wrong tops)")
    if args.show:
        for row in best[1].rows:
            print(f"  {row['query'][:70]:72} expected {row['expected']} got {row['got']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
