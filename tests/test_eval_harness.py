import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_triage import run_eval


def test_eval_harness_runs_on_small_dataset(tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    rows = [
        {
            "service": "payments-service",
            "metric": "latency_p99_ms",
            "value": 1200,
            "threshold": 800,
            "environment": "prod",
            "message": "Checkout latency increased",
            "expected_issue_type": "performance_degradation",
        },
        {
            "service": "orders-service",
            "metric": "error_rate_pct",
            "value": 8.2,
            "threshold": 2.0,
            "environment": "prod",
            "message": "5xx error spike detected",
            "expected_issue_type": "application_error",
        },
    ]
    dataset.write_text("\n".join(json.dumps(item) for item in rows), encoding="utf-8")

    report = run_eval(dataset_path=dataset, mode="heuristic", confidence_floor=0.60)

    assert report["case_count"] == 2
    assert report["accuracy"] == 1.0
    assert report["correct"] == 2
