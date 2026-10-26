"""Replay known good and bad fixes through the Verifier and print a scorecard.

    PYTHONPATH=src python scripts/replay_verifications.py

Each case builds a throwaway git repo, applies a fix (some of them dishonest or careless), deploys it
into a simulated dev environment and checks the Verifier's verdict against the known answer. The
cases are mock data: this is a regression check on the Verifier's logic, not a measure of how it does
on real fixes.
Exits non-zero if the Verifier passes a fix that should not pass, deploys something an earlier check
should have stopped, or exposes a secret in a report.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.verification_replay import run_verification_replay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=str(ROOT / "eval" / "verification_report.json"))
    args = parser.parse_args()

    summary = run_verification_replay()

    print(f"{'case':38}{'expected':14}{'got':14}{'deployed':10} failed checks")
    for r in summary.results:
        mark = "ok " if r.correct else "BAD"
        print(f"{r.case:38}{r.expected_verdict:14}{r.verdict:14}{str(r.deployed):10} {', '.join(r.failed_checks) or '-'}  [{mark}]")
    data = summary.to_dict()
    print(
        f"\ncorrect verdicts {data['accuracy']:.0%} | false passes {data['false_passes']} | "
        f"false fails {data['false_fails']} | wrongly deployed {data['wrongly_deployed']} | "
        f"secret leaks {data['secret_leaks']}"
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"report written to {output}")

    return 1 if (data["false_passes"] or data["wrongly_deployed"] or data["secret_leaks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
