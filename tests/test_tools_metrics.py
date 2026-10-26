import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from app.tools.base import TimeWindow, ToolError
from app.tools.metrics import DEFAULT_PROMQL_CATALOG, MockMetricsTool, PrometheusMetricsTool, PromQuery
from app.tools.scenarios import ANCHOR, BAD_DEPLOY_ERROR_SPIKE, DEFAULT_SCENARIOS


def _window(minutes=45, end=ANCHOR):
    return TimeWindow.last(minutes, end=end)


def test_mock_series_is_flat_before_onset_and_peaks_after_it():
    series = MockMetricsTool().query("orders-service", "error_rate_pct", _window())

    before = [p.value for p in series.points if p.timestamp < ANCHOR - timedelta(minutes=33)]
    after = [p.value for p in series.points if p.timestamp > ANCHOR - timedelta(minutes=20)]
    assert max(before) < 1.0
    assert min(after) > 6.5
    assert series.unit == "percent"
    assert series.summary()["max"] > 7.0 > series.summary()["first"]


def test_mock_series_is_deterministic():
    tool = MockMetricsTool()
    first = tool.query("orders-service", "error_rate_pct", _window())
    second = tool.query("orders-service", "error_rate_pct", _window())

    assert first == second


def test_mock_lists_metrics_and_rejects_unknown_ones():
    tool = MockMetricsTool()

    assert tool.available_metrics("orders-service") == ["cpu_pct", "error_rate_pct", "latency_p95_ms"]
    assert tool.available_metrics("unknown-service") == []
    with pytest.raises(ToolError, match="unknown metric 'nope'"):
        tool.query("orders-service", "nope", _window())


def test_a_service_without_a_scenario_is_reported_healthy_not_invented():
    series = MockMetricsTool().query("billing-service", "latency_p95_ms", _window(5))

    assert {p.value for p in series.points} == {1.0}


def test_step_controls_the_number_of_points():
    series = MockMetricsTool().query("orders-service", "cpu_pct", _window(10), step_seconds=120)

    assert len(series.points) == 6  # 0, 2, 4, 6, 8, 10 minutes


def test_time_window_validation():
    with pytest.raises(ValueError, match="before end"):
        TimeWindow(start=ANCHOR, end=ANCHOR - timedelta(minutes=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        TimeWindow(start=datetime(2026, 1, 1), end=datetime(2026, 1, 2))


# --- Prometheus over real HTTP ------------------------------------------------------------


@pytest.fixture
def prometheus_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    state = {"requests": [], "status": 200, "body": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(urlparse(self.path))
            payload = json.dumps(state["body"]).encode("utf-8")
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


def _matrix(values):
    return {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": values}]}}


def test_prometheus_runs_the_catalog_query_and_parses_the_matrix(prometheus_server):
    start = ANCHOR - timedelta(minutes=10)
    prometheus_server["body"] = _matrix(
        [[start.timestamp(), "0.31"], [start.timestamp() + 60, "NaN"], [start.timestamp() + 120, "2.2"]]
    )
    tool = PrometheusMetricsTool(prometheus_server["url"])

    series = tool.query("faulty-project", "latency_p95_seconds", TimeWindow(start=start, end=ANCHOR), step_seconds=60)

    assert [p.value for p in series.points] == [0.31, 2.2]  # the NaN step is dropped
    assert series.points[0].timestamp == start
    assert series.unit == "seconds"
    request = prometheus_server["requests"][0]
    assert request.path == "/api/v1/query_range"
    query = parse_qs(request.query)
    assert query["query"] == [DEFAULT_PROMQL_CATALOG[("faulty-project", "latency_p95_seconds")].expr]
    assert query["step"] == ["60"]
    assert float(query["end"][0]) == ANCHOR.timestamp()


def test_prometheus_never_runs_a_query_that_is_not_in_the_catalog(prometheus_server):
    tool = PrometheusMetricsTool(prometheus_server["url"])

    with pytest.raises(ToolError, match="no query defined for faulty-project/up{} or vector"):
        tool.query("faulty-project", "up{} or vector(1)", _window())
    assert prometheus_server["requests"] == []
    assert tool.available_metrics("faulty-project") == ["error_rate_per_sec", "latency_p95_seconds"]


def test_prometheus_query_failure_is_a_readable_tool_error(prometheus_server):
    prometheus_server["body"] = {"status": "error", "errorType": "bad_data", "error": "parse error at char 5"}
    tool = PrometheusMetricsTool(prometheus_server["url"])

    with pytest.raises(ToolError, match="parse error at char 5"):
        tool.query("sample-app", "error_rate_per_sec", _window())


def test_prometheus_http_error_and_unreachable_server_are_tool_errors(prometheus_server):
    prometheus_server["status"] = 503
    prometheus_server["body"] = {"status": "error"}
    tool = PrometheusMetricsTool(prometheus_server["url"])
    with pytest.raises(ToolError, match="Prometheus error 503"):
        tool.query("sample-app", "error_rate_per_sec", _window())

    dead = PrometheusMetricsTool("http://127.0.0.1:9", timeout_seconds=1)
    with pytest.raises(ToolError, match="Prometheus connection error"):
        dead.query("sample-app", "error_rate_per_sec", _window())


def test_prometheus_empty_result_is_an_empty_series(prometheus_server):
    prometheus_server["body"] = {"status": "success", "data": {"resultType": "matrix", "result": []}}

    series = PrometheusMetricsTool(prometheus_server["url"]).query("sample-app", "error_rate_per_sec", _window())

    assert series.points == []
    assert series.summary() == {}


def test_prometheus_requires_a_url_and_accepts_a_custom_catalog():
    with pytest.raises(ValueError, match="base URL"):
        PrometheusMetricsTool("")
    tool = PrometheusMetricsTool("http://x", catalog={("svc", "m"): PromQuery("up")})
    assert tool.available_metrics("svc") == ["m"]


def test_default_scenarios_are_internally_consistent():
    for scenario in DEFAULT_SCENARIOS:
        assert scenario.alert_metric in scenario.metrics, scenario.name
        assert MockMetricsTool().query(scenario.service, scenario.alert_metric, _window()).points
        shape = scenario.metrics[scenario.alert_metric]
        assert shape.peak > shape.baseline


def test_a_deploy_precedes_the_onset_in_the_deploy_correlated_scenario():
    deploy = BAD_DEPLOY_ERROR_SPIKE.deploys[0]
    onset = ANCHOR - timedelta(minutes=BAD_DEPLOY_ERROR_SPIKE.metrics["error_rate_pct"].onset_minutes)

    assert deploy.deployed_at < onset
