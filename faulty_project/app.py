import json
import random
import time
from pathlib import Path

from flask import Flask, jsonify
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

app = Flask(__name__)
CONFIG_PATH = Path("/app/app_config.json")

REQ_COUNT = Counter(
    "faulty_requests_total",
    "Faulty app request count",
    ["endpoint", "status"],
)
REQ_DURATION = Histogram(
    "faulty_request_duration_seconds",
    "Faulty app request latency",
    ["endpoint"],
    buckets=(0.1, 0.3, 0.5, 1, 2, 3, 5),
)


def load_cfg() -> dict:
    if not CONFIG_PATH.exists():
        return {"latency_ms": 1800, "error_probability": 0.25}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"latency_ms": 1800, "error_probability": 0.25}


@app.get("/api/checkout")
def checkout():
    cfg = load_cfg()
    delay_ms = float(cfg.get("latency_ms", 1800))
    error_prob = float(cfg.get("error_probability", 0.25))

    started = time.time()
    time.sleep(max(0.0, random.uniform(delay_ms * 0.7, delay_ms * 1.3) / 1000.0))
    elapsed = time.time() - started
    REQ_DURATION.labels(endpoint="checkout").observe(elapsed)

    if random.random() < error_prob:
        REQ_COUNT.labels(endpoint="checkout", status="error").inc()
        return jsonify({"status": "error", "message": "intentional faulty checkout"}), 500

    REQ_COUNT.labels(endpoint="checkout", status="success").inc()
    return jsonify({"status": "ok", "latency_ms": round(elapsed * 1000.0, 2)})


@app.get("/api/health")
def health():
    cfg = load_cfg()
    return jsonify({"status": "ok", "config": cfg})


@app.get("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8010)
