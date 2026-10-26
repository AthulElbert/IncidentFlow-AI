"""Replay the built-in incident scenarios through the Investigator and print a scorecard.

    PYTHONPATH=src python scripts/replay_investigations.py
    PYTHONPATH=src python scripts/replay_investigations.py --mode llm      # needs LLM_API_KEY
    PYTHONPATH=src python scripts/replay_investigations.py --no-history    # ablate incident memory

The scenarios are mock data with known answers. The scorecard is a regression check for the
Investigator's logic, not a measurement of how it performs on real incidents.
Exits non-zero if the Investigator proceeds when it should not have (a wrong answer, or an alert only a
human can resolve), or (unless --allow-regression) if accuracy drops below the keyword baseline.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.adapters.llm_client import OpenAICompatibleLLMClient  # noqa: E402
from app.agents.investigator import Investigator  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.replay import run_replay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--no-history", action="store_true", help="run with an empty incident memory")
    parser.add_argument("--output", default=str(ROOT / "eval" / "investigation_report.json"))
    parser.add_argument("--allow-regression", action="store_true")
    args = parser.parse_args()

    settings = load_settings(str(ROOT))
    llm = None
    if args.mode == "llm":
        if not settings.llm_api_key:
            print("LLM_API_KEY is required for --mode llm", file=sys.stderr)
            return 2
        llm = OpenAICompatibleLLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.investigator_model or settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            verify_ssl=settings.llm_verify_ssl,
        )

    investigator = Investigator(
        llm=llm,
        min_confidence=settings.investigation_min_confidence,
        min_margin=settings.investigation_min_margin,
        max_llm_steps=settings.investigation_max_llm_steps,
    )
    summary = run_replay(investigator, history=[] if args.no_history else None)

    print(f"{'scenario':26}{'expected':25}{'got':25}{'conf':>6}  {'decision':9} baseline")
    for r in summary.results:
        mark = "BAD" if r.outcome in ("proceeded_wrong", "proceeded_unsafely") else "ok "
        base = "ok" if r.baseline_correct else f"wrong ({r.baseline_issue_type})"
        print(f"{r.scenario:26}{r.expected_issue_type:25}{r.predicted_issue_type:25}{r.confidence:>6.2f}  {r.decision:9} {base}  [{mark}]")
    data = summary.to_dict()
    print(
        f"\naccuracy {data['accuracy']:.0%} (baseline {data['baseline_accuracy']:.0%}) | "
        f"automated correctly {data['automation_rate']:.0%} | harmful {data['harmful']} | "
        f"unneeded escalations {data['unneeded_escalations']} | "
        f"root-cause keywords {data['root_cause_hit_rate']:.0%} | mode {args.mode}"
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"report written to {output}")

    if data["harmful"]:
        return 1
    if not args.allow_regression and data["accuracy"] < data["baseline_accuracy"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
