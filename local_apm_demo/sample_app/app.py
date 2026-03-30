import random
import time

from flask import Flask, jsonify
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

app = Flask(__name__)

REQ_COUNT = Counter(
    "app_requests_total",
    "Total number of requests processed",
    ["endpoint", "status"],
)
REQ_DURATION = Histogram(
    "app_request_duration_seconds",
    "Request duration in seconds",
    ["endpoint"],
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 3, 5),
)


def _track(endpoint: str, status: str, started: float) -> None:
    REQ_COUNT.labels(endpoint=endpoint, status=status).inc()
    REQ_DURATION.labels(endpoint=endpoint).observe(time.time() - started)


@app.get("/api/ok")
def ok():
    started = time.time()
    time.sleep(random.uniform(0.01, 0.08))
    _track("ok", "success", started)
    return jsonify({"status": "ok"})


@app.get("/api/slow")
def slow():
    started = time.time()
    time.sleep(random.uniform(1.5, 2.8))
    _track("slow", "success", started)
    return jsonify({"status": "slow", "message": "intentional latency endpoint"})


@app.get("/api/error")
def error():
    started = time.time()
    time.sleep(random.uniform(0.03, 0.12))
    _track("error", "error", started)
    return jsonify({"status": "error", "message": "intentional failure endpoint"}), 500


@app.get("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)
