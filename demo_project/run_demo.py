import argparse
import json
from datetime import datetime, timezone
from urllib import error, request


def _call(
    method: str,
    url: str,
    api_key: str,
    payload: dict | None = None,
) -> tuple[int, dict | list | str]:
    data = None
    headers = {"Accept": "application/json", "X-API-Key": api_key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url=url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
            if not text.strip():
                return resp.status, {}
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                return resp.status, text
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return exc.code, detail
    except Exception as exc:  # pragma: no cover
        return 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a simple end-to-end demo check for the agent platform.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--viewer-key", default="viewer-local-key", help="Viewer API key")
    parser.add_argument("--approver-key", default="approver-local-key", help="Approver API key")
    parser.add_argument("--print-change-json", action="store_true", help="Print full latest change JSON")
    args = parser.parse_args()

    print("1) Health check")
    code, body = _call("GET", f"{args.base_url}/health", api_key=args.viewer_key)
    if code != 200:
        print(f"FAILED health: code={code} body={body}")
        return 1
    print(f"OK health: scheduler_running={body.get('scheduler_running')} apm_poll_mode={body.get('apm_poll_mode')}")

    print("2) Queue a scheduler alert")
    alert_payload = {
        "service": "sample-app",
        "metric": "latency_p95_seconds",
        "value": 1.9,
        "threshold": 1.2,
        "environment": "local",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "Demo alert: sample app latency above threshold",
    }
    code, body = _call("POST", f"{args.base_url}/v1/apm/mock-alerts", api_key=args.approver_key, payload=alert_payload)
    if code != 200:
        print(f"FAILED queue alert: code={code} body={body}")
        return 1
    source_alert_id = body.get("source_alert_id", "n/a")
    print(f"OK queued alert: source_alert_id={source_alert_id}")

    print("3) Trigger scheduler run-once")
    code, body = _call("POST", f"{args.base_url}/v1/scheduler/run-once", api_key=args.approver_key)
    if code != 200:
        print(f"FAILED scheduler run: code={code} body={body}")
        return 1
    stats = (body or {}).get("stats", {})
    print(
        "OK scheduler: "
        f"runs={stats.get('runs')} processed={stats.get('alerts_processed')} "
        f"dedup_skipped={stats.get('alerts_skipped_dedup')} failed={stats.get('alerts_failed')}"
    )

    print("4) Fetch latest changes")
    code, body = _call("GET", f"{args.base_url}/v1/changes", api_key=args.viewer_key)
    if code != 200 or not isinstance(body, list):
        print(f"FAILED changes: code={code} body={body}")
        return 1
    if not body:
        print("FAILED: no changes found after scheduler run")
        return 1
    latest = body[-1]
    print(
        "OK latest change: "
        f"change_id={latest.get('change_id')} issue_type={latest.get('issue_type')} "
        f"status={latest.get('status')} deployment_state={latest.get('deployment_state')}"
    )
    if args.print_change_json:
        print(json.dumps(latest, indent=2))

    print("5) Fetch metrics summary")
    code, body = _call("GET", f"{args.base_url}/v1/metrics/summary", api_key=args.viewer_key)
    if code != 200 or not isinstance(body, dict):
        print(f"FAILED metrics: code={code} body={body}")
        return 1
    print(
        "OK metrics: "
        f"total_changes={body.get('total_changes')} "
        f"warning_rate={body.get('warning_rate')} "
        f"policy_block_rate={body.get('policy_block_rate')}"
    )

    print("DEMO CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
