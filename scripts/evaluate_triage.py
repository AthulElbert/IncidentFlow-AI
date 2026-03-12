import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from app.adapters.llm_client import OpenAICompatibleLLMClient
from app.models.schemas import APMEvent
from app.services.triage_agent import TriageAgent


@dataclass
class EvalCaseResult:
    service: str
    metric: str
    message: str
    expected_issue_type: str
    predicted_issue_type: str
    confidence: float
    mode_used: str
    correct: bool


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _build_agent(mode: str, confidence_floor: float) -> TriageAgent:
    if mode != "llm":
        return TriageAgent(mode="heuristic", llm_client=None, confidence_floor=confidence_floor)

    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise ValueError("LLM_API_KEY is required when --mode llm")
    client = OpenAICompatibleLLMClient(
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key,
        model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
        timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "20")),
        verify_ssl=os.getenv("LLM_VERIFY_SSL", "true").strip().lower() in {"1", "true", "yes", "on"},
    )
    return TriageAgent(mode="llm", llm_client=client, confidence_floor=confidence_floor)


def run_eval(dataset_path: Path, mode: str, confidence_floor: float) -> dict:
    rows = _load_jsonl(dataset_path)
    agent = _build_agent(mode=mode, confidence_floor=confidence_floor)
    results: list[EvalCaseResult] = []
    confusion: dict[str, Counter[str]] = {}

    for row in rows:
        event = APMEvent(
            service=row["service"],
            metric=row["metric"],
            value=float(row["value"]),
            threshold=float(row["threshold"]),
            environment=row.get("environment", "prod"),
            timestamp="2026-03-01T12:00:00Z",
            message=row["message"],
        )
        expected = str(row["expected_issue_type"])
        triage = agent.triage(event)
        predicted = triage.issue_type
        correct = predicted == expected

        if expected not in confusion:
            confusion[expected] = Counter()
        confusion[expected][predicted] += 1

        results.append(
            EvalCaseResult(
                service=event.service,
                metric=event.metric,
                message=event.message,
                expected_issue_type=expected,
                predicted_issue_type=predicted,
                confidence=triage.confidence,
                mode_used=triage.mode_used,
                correct=correct,
            )
        )

    total = len(results)
    correct_total = sum(1 for item in results if item.correct)
    accuracy = round(correct_total / total, 4) if total else 0.0

    return {
        "dataset": str(dataset_path),
        "mode_requested": mode,
        "case_count": total,
        "accuracy": accuracy,
        "correct": correct_total,
        "incorrect": total - correct_total,
        "per_expected_label": {
            label: {
                "total": sum(counter.values()),
                "correct": counter.get(label, 0),
                "accuracy": round(counter.get(label, 0) / sum(counter.values()), 4) if sum(counter.values()) else 0.0,
            }
            for label, counter in confusion.items()
        },
        "confusion_matrix": {label: dict(counter) for label, counter in confusion.items()},
        "results": [asdict(item) for item in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate triage quality against a labeled dataset.")
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "eval" / "triage_dataset.jsonl"),
        help="Path to JSONL dataset.",
    )
    parser.add_argument("--mode", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--confidence-floor", type=float, default=0.60)
    parser.add_argument(
        "--output",
        default=str(ROOT / "eval" / "triage_eval_report.json"),
        help="Where to write evaluation report JSON.",
    )
    args = parser.parse_args()

    report = run_eval(
        dataset_path=Path(args.dataset),
        mode=args.mode,
        confidence_floor=args.confidence_floor,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"mode={report['mode_requested']} cases={report['case_count']} accuracy={report['accuracy']}")
    print(f"report={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
