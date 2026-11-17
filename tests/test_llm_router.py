"""Routing model calls by task: fallback, retries, the circuit breaker, and what each call cost."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from agent_harness import APPROVER, CONFIG_PATH, FIXED, INVALID, VIEWER, build, done, edit

from app.adapters.llm_client import LLMError, LLMReply, OpenAICompatibleLLMClient
from app.adapters.llm_router import (
    CircuitBreaker,
    Endpoint,
    Escalation,
    LLMRouter,
    RoutingConfigError,
    load_routing_file,
)
from app.adapters.llm_usage import LLMCall, llm_task
from app.agents.budget import BudgetLimits
from app.audit import new_audit_entry
from app.config import ConfigError, load_settings, validate_settings
from app.models.schemas import APMEvent
from app.services.integration_factory import (
    build_critic_llm,
    build_fixer_llm,
    build_investigator_llm,
    build_llm_client,
    llm_for_profile,
)
from app.services.review import build_timeline


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeBackend:
    """Plays back replies and errors in order; the last one repeats."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.seen: list[list[dict]] = []

    def complete(self, messages, error_label="LLM"):
        self.calls += 1
        self.seen.append(messages)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def ok(content=None, prompt=100, completion=20):
    return LLMReply(content or {"answer": "yes"}, prompt, completion)


def transient(message="LLM API error 503: busy"):
    return LLMError(message, status=503, transient=True)


def fatal(message="LLM API error 400: bad request"):
    return LLMError(message, status=400, transient=False)


def make_router(backends: dict, profiles: dict, clock=None, **kwargs):
    sleeps: list[float] = []
    endpoints = {
        name: Endpoint(name, backend, f"{name}-model", *prices)
        for name, (backend, prices) in ((n, v if isinstance(v, tuple) else (v, (0.0, 0.0))) for n, v in backends.items())
    }
    router = LLMRouter(endpoints, profiles, sleep=sleeps.append, clock=clock or Clock(), jitter=lambda: 1.0, **kwargs)
    return router, sleeps


def collect():
    calls: list[LLMCall] = []
    return calls, calls.append


# --- one endpoint ---------------------------------------------------------------------------


def test_a_call_returns_the_reply_and_reports_tokens_and_cost():
    backend = FakeBackend(ok(prompt=1000, completion=500))
    router, _ = make_router({"main": (backend, (2.0, 8.0))}, {"default": ["main"]})
    calls, sink = collect()

    with llm_task(sink=sink):
        reply = router.client().complete_json([{"role": "user", "content": "hi"}])

    assert reply == {"answer": "yes"}
    [call] = calls
    assert (call.profile, call.endpoint, call.model, call.ok) == ("default", "main", "main-model", True)
    assert (call.prompt_tokens, call.completion_tokens, call.tokens, call.usage_reported) == (1000, 500, 1500, True)
    assert call.cost == pytest.approx((1000 * 2.0 + 500 * 8.0) / 1_000_000)


def test_a_provider_that_reports_no_usage_counts_as_zero_tokens_and_says_so():
    router, _ = make_router({"main": FakeBackend(LLMReply({"a": 1}))}, {"default": ["main"]})
    calls, sink = collect()

    with llm_task(sink=sink):
        router.client().complete_json([])

    assert calls[0].tokens == 0 and calls[0].usage_reported is False and calls[0].cost == 0


def test_a_transient_error_is_retried_on_the_same_endpoint_with_growing_backoff():
    backend = FakeBackend(transient(), transient(), ok())
    router, sleeps = make_router({"main": backend}, {"default": ["main"]}, max_retries=2)
    calls, sink = collect()

    with llm_task(sink=sink):
        assert router.client().complete_json([]) == {"answer": "yes"}

    assert backend.calls == 3 and sleeps == [0.5, 1.0]
    assert [c.ok for c in calls] == [False, False, True] and "503" in calls[0].error


def test_an_error_that_will_not_fix_itself_is_not_retried_and_keeps_its_own_message():
    backend = FakeBackend(fatal())
    router, sleeps = make_router({"main": backend}, {"default": ["main"]})

    with pytest.raises(LLMError, match="LLM API error 400: bad request"):
        router.client().complete_json([])

    assert backend.calls == 1 and sleeps == []


def rate_limited(wait):
    return LLMError("LLM API error 429: slow down", status=429, transient=True, retry_after=wait)


def test_a_rate_limit_waits_as_long_as_the_provider_asks_and_does_not_count_as_a_broken_endpoint():
    backend = FakeBackend(rate_limited(41), rate_limited(41), ok())
    router, sleeps = make_router({"main": backend}, {"default": ["main"]}, max_retries=3, breaker_threshold=1)

    assert router.client().complete_json([]) == {"answer": "yes"}

    assert sleeps == [41, 41]  # the provider's wait, not the short backoff
    assert router.status()["endpoints"]["main"]["circuit"] == "closed"  # a threshold of 1 would have opened it


def test_a_requested_wait_too_long_for_an_agent_run_moves_on_instead_of_sitting_it_out():
    primary, backup = FakeBackend(rate_limited(3600)), FakeBackend(ok({"from": "backup"}))
    router, sleeps = make_router(
        {"primary": primary, "backup": backup}, {"default": ["primary", "backup"]}, max_retry_after_seconds=60
    )

    assert router.client().complete_json([]) == {"from": "backup"}
    assert primary.calls == 1 and sleeps == []


def test_retries_stop_at_the_limit():
    backend = FakeBackend(transient())
    router, sleeps = make_router({"main": backend}, {"default": ["main"]}, max_retries=1, breaker_threshold=10)

    with pytest.raises(LLMError, match="503"):
        router.client().complete_json([])

    assert backend.calls == 2 and len(sleeps) == 1


# --- fallback ---------------------------------------------------------------------------------


def test_when_the_first_choice_fails_the_next_endpoint_answers():
    primary, backup = FakeBackend(fatal()), FakeBackend(ok({"from": "backup"}))
    router, _ = make_router({"primary": primary, "backup": backup}, {"default": ["primary", "backup"]})
    calls, sink = collect()

    with llm_task(sink=sink):
        assert router.client().complete_json([]) == {"from": "backup"}

    assert [(c.endpoint, c.ok) for c in calls] == [("primary", False), ("backup", True)]


def test_a_flaky_first_choice_is_retried_before_falling_back():
    primary, backup = FakeBackend(transient()), FakeBackend(ok({"from": "backup"}))
    router, _ = make_router({"primary": primary, "backup": backup}, {"default": ["primary", "backup"]}, max_retries=1, breaker_threshold=10)

    assert router.client().complete_json([]) == {"from": "backup"}
    assert primary.calls == 2 and backup.calls == 1


def test_if_every_endpoint_fails_the_error_names_each_one():
    router, _ = make_router(
        {"primary": FakeBackend(fatal("LLM API error 401: no key")), "backup": FakeBackend(fatal("LLM API error 404: no model"))},
        {"default": ["primary", "backup"]},
    )

    with pytest.raises(LLMError) as raised:
        router.client().complete_json([], error_label="fixer")

    text = str(raised.value)
    assert "every model endpoint failed" in text and "primary: LLM API error 401" in text and "backup: LLM API error 404" in text


# --- circuit breaker ----------------------------------------------------------------------------


def test_the_breaker_opens_after_repeated_failures_and_lets_one_call_through_after_the_cooldown():
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=30, clock=clock)
    assert breaker.state == "closed" and breaker.allow()

    breaker.failure()
    assert breaker.allow()
    breaker.failure()
    assert breaker.state == "open" and not breaker.allow()

    clock.now = 31
    assert breaker.state == "half-open" and breaker.allow()
    breaker.failure()  # the trial call failed: closed for another cooldown
    assert breaker.state == "open"
    clock.now = 62
    breaker.success()
    assert breaker.state == "closed"


def test_an_endpoint_that_keeps_failing_is_skipped_until_it_has_had_time_to_recover():
    clock = Clock()
    primary, backup = FakeBackend(fatal(), fatal(), ok({"from": "primary"})), FakeBackend(ok({"from": "backup"}))
    router, _ = make_router(
        {"primary": primary, "backup": backup}, {"default": ["primary", "backup"]},
        clock=clock, breaker_threshold=2, breaker_cooldown_seconds=30,
    )

    router.client().complete_json([])
    router.client().complete_json([])
    assert primary.calls == 2 and router.status()["endpoints"]["primary"]["circuit"] == "open"

    router.client().complete_json([])  # the primary is not even tried
    assert primary.calls == 2 and backup.calls == 3

    clock.now = 31
    assert router.client().complete_json([]) == {"from": "primary"}
    assert router.status()["endpoints"]["primary"]["circuit"] == "closed"


def test_when_nothing_is_available_the_call_fails_fast_and_is_marked_retryable():
    clock = Clock()
    backend = FakeBackend(fatal())
    router, _ = make_router({"main": backend}, {"default": ["main"]}, clock=clock, breaker_threshold=1)
    with pytest.raises(LLMError):
        router.client().complete_json([])

    with pytest.raises(LLMError, match="no model endpoint is available") as raised:
        router.client().complete_json([])

    assert raised.value.transient and backend.calls == 1


# --- profiles -----------------------------------------------------------------------------------


def test_each_task_uses_its_own_profile_and_an_unrouted_task_shares_the_default():
    cheap, strong = FakeBackend(ok({"by": "cheap"})), FakeBackend(ok({"by": "strong"}))
    router, _ = make_router({"cheap": cheap, "strong": strong}, {"default": ["cheap"], "critic.review": ["strong"]})

    assert router.client("critic.review").complete_json([]) == {"by": "strong"}
    assert router.client("fixer.step") is router.client() and router.client().model == "cheap-model"
    assert router.client("critic.review").model == "strong-model" and router.client("critic.review") is not router.client()
    assert router.has_profile("critic.review") and not router.has_profile("fixer.step")


def test_a_retry_of_a_failed_fix_can_be_sent_to_a_stronger_model():
    cheap, strong = FakeBackend(ok({"by": "cheap"})), FakeBackend(ok({"by": "strong"}))
    router, _ = make_router(
        {"cheap": cheap, "strong": strong}, {"default": ["cheap"], "fixer.step": ["cheap", "strong"]},
        escalations={"fixer.step": Escalation(attempt_gte=2, use="strong")},
    )
    client = router.client("fixer.step")

    with llm_task(attempt=1):
        assert client.complete_json([]) == {"by": "cheap"}
    with llm_task(attempt=2):
        assert client.complete_json([]) == {"by": "strong"}
    assert [e.name for e in router.chain("fixer.step", attempt=3)] == ["strong", "cheap"]


def test_the_triage_and_code_fix_helpers_go_through_the_router_too():
    backend = FakeBackend(ok({"issue_type": "unknown"}))
    router, _ = make_router({"main": backend}, {"default": ["main"], "triage": ["main"]})
    event = APMEvent(service="s", metric="m", value=2, threshold=1, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), message="x")
    calls, sink = collect()

    with llm_task(sink=sink):
        router.client("triage").triage(event)
        router.client("code_fix").propose_code_fix("a.json", "{}", "fix it")

    assert [c.profile for c in calls] == ["triage", "default"]
    assert "SRE triage assistant" in backend.seen[0][0]["content"] and "a.json" in backend.seen[1][1]["content"]


# --- accounting never mixes runs and never breaks a call -----------------------------------------


def test_two_tasks_running_at_once_each_see_only_their_own_calls():
    router, _ = make_router({"main": FakeBackend(ok())}, {"default": ["main"]})
    start = threading.Barrier(2)

    def task(count):
        calls, sink = collect()
        with llm_task(sink=sink):
            start.wait(5)
            for _ in range(count):
                router.client().complete_json([])
        return len(calls)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(task, [3, 7])) == [3, 7]


def test_a_failing_usage_sink_does_not_fail_the_model_call():
    router, _ = make_router({"main": FakeBackend(ok())}, {"default": ["main"]})

    def broken(_call):
        raise RuntimeError("audit store is down")

    with llm_task(sink=broken):
        assert router.client().complete_json([]) == {"answer": "yes"}


def test_calls_outside_any_task_are_simply_not_recorded():
    router, _ = make_router({"main": FakeBackend(ok())}, {"default": ["main"]})

    assert router.client().complete_json([]) == {"answer": "yes"}


# --- the routing file ------------------------------------------------------------------------------


ROUTING = {
    "endpoints": {
        "strong": {"base_url": "https://a.example/v1", "model": "big", "api_key_env": "KEY_A", "price_per_mtok": [2.0, 8.0]},
        "local": {"base_url": "http://localhost:11434/v1", "model": "small", "local": True},
    },
    "profiles": {"default": ["strong"], "triage": ["local"], "fixer.step": ["local", "strong"]},
    "escalate_on_retry": {"fixer.step": {"attempt_gte": 2, "use": "strong"}},
}


def _routing_file(tmp_path, **changes):
    data = json.loads(json.dumps(ROUTING))
    data.update(changes)
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_routing_file_builds_endpoints_profiles_and_prices_without_holding_any_key(tmp_path):
    router = load_routing_file(_routing_file(tmp_path), {"KEY_A": "secret-a"})

    assert router.client().model == "big" and router.client("triage").model == "small"
    assert router.endpoints["strong"].cost(1_000_000, 0) == 2.0 and router.endpoints["local"].local
    assert [e.name for e in router.chain("fixer.step", attempt=2)] == ["strong", "local"]
    status = json.dumps(router.status())
    assert "secret-a" not in status and "KEY_A" not in status


@pytest.mark.parametrize(
    "changes,env,message",
    [
        ({"profiles": {"triage": ["local"]}}, {"KEY_A": "k"}, "needs a 'default' profile"),
        ({"profiles": {"default": ["nope"]}}, {"KEY_A": "k"}, "unknown endpoint 'nope'"),
        ({"profiles": {"default": ["strong"], "fixer.stp": ["strong"]}}, {"KEY_A": "k"}, "unknown LLM profile 'fixer.stp'"),
        ({"profiles": {"default": []}}, {"KEY_A": "k"}, "lists no endpoints"),
        ({}, {}, "needs api_key_env naming a set environment variable"),
        ({"escalate_on_retry": {"fixer.step": {"attempt_gte": 1, "use": "strong"}}}, {"KEY_A": "k"}, "attempt_gte of 2 or more"),
        ({"escalate_on_retry": {"fixer.step": {"attempt_gte": 2, "use": "ghost"}}}, {"KEY_A": "k"}, "unknown endpoint 'ghost'"),
        ({"local_only_profiles": ["fixer.step"]}, {"KEY_A": "k"}, "local-only but can reach non-local endpoint"),
        ({"local_only_profiles": ["critic.review"]}, {"KEY_A": "k"}, "local_only_profiles names unknown profile"),
    ],
)
def test_a_mistake_in_the_routing_file_is_refused_with_the_reason(tmp_path, changes, env, message):
    with pytest.raises(RoutingConfigError, match=message):
        load_routing_file(_routing_file(tmp_path, **changes), env)


def test_a_local_only_task_is_accepted_when_every_route_stays_inside(tmp_path):
    router = load_routing_file(_routing_file(tmp_path, local_only_profiles=["triage"]), {"KEY_A": "k"})

    assert [e.name for e in router.chain("triage")] == ["local"]


def test_a_missing_or_malformed_routing_file_is_reported(tmp_path):
    with pytest.raises(RoutingConfigError, match="cannot read"):
        load_routing_file(tmp_path / "nope.json", {})
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RoutingConfigError, match="not valid JSON"):
        load_routing_file(bad, {})
    prices = _routing_file(tmp_path)
    data = json.loads(prices.read_text())
    data["endpoints"]["strong"]["price_per_mtok"] = [1]
    prices.write_text(json.dumps(data))
    with pytest.raises(RoutingConfigError, match="price_per_mtok"):
        load_routing_file(prices, {"KEY_A": "k"})


# --- settings and the factory -------------------------------------------------------------------------


def test_with_a_routing_file_each_agent_gets_its_profile_and_no_main_key_is_needed(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTING_FILE", str(_routing_file(tmp_path)))
    monkeypatch.setenv("KEY_A", "secret-a")
    for mode in ("FIXER_MODE", "CRITIC_MODE", "INVESTIGATOR_MODE"):
        monkeypatch.setenv(mode, "llm")
    monkeypatch.setenv("ALLOW_HOST_EXECUTION", "true")
    settings = load_settings()
    validate_settings(settings)  # no LLM_API_KEY, and that is fine

    shared, _ = build_llm_client(settings)
    fixer, critic = build_fixer_llm(settings, shared), build_critic_llm(settings, shared)

    assert shared.model == "big" and fixer.profile == "fixer.step" and fixer.model == "small"
    assert critic is shared  # no profile of its own, so it shares the default
    assert build_investigator_llm(settings, shared) is shared
    assert llm_for_profile(shared, "triage").model == "small" and llm_for_profile(None, "triage") is None


@pytest.mark.parametrize(
    "env,message",
    [
        ({"LLM_ROUTING_FILE": "ROUTING", "KEY_A": "k", "FIXER_MODEL": "x"}, "both choose models"),
        ({"LLM_ROUTING_FILE": "ROUTING"}, "LLM_ROUTING_FILE: endpoint 'strong' needs api_key_env"),
        ({"LLM_ROUTING_FILE": "does-not-exist.json"}, "LLM_ROUTING_FILE: cannot read"),
        ({"LLM_MAX_RETRIES": "9"}, "LLM_MAX_RETRIES"),
        ({"AGENT_RUN_MAX_TOKENS": "-1"}, "AGENT_RUN_MAX_TOKENS"),
    ],
)
def test_bad_routing_settings_stop_the_app_at_startup(monkeypatch, tmp_path, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, str(_routing_file(tmp_path)) if value == "ROUTING" else value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_a_routing_file_where_agents_can_edit_is_refused(monkeypatch):
    from app.config import PROJECT_ROOT

    monkeypatch.setenv("LLM_ROUTING_FILE", "ops/llm_routing.example.json")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "faulty_project/,ops/")

    with pytest.raises(ConfigError, match="inside 'ops/', which agents may edit"):
        validate_settings(load_settings())

    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "faulty_project/,config/")
    validate_settings(load_settings())  # the shipped example is valid and sits outside the editable paths
    assert (PROJECT_ROOT / "ops" / "llm_routing.example.json").exists()


def test_the_default_budget_now_limits_tokens_but_not_money():
    settings = load_settings()

    assert (settings.agent_run_max_tokens, settings.agent_run_max_cost, settings.llm_max_retries) == (600_000, 0.0, 2)


# --- the real HTTP client: usage and which errors are worth retrying -----------------------------------


@pytest.fixture
def llm_server():
    state = {"status": 200, "body": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in state.get("headers", {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()


def _chat(content, usage=None):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def test_the_http_client_returns_the_token_usage_the_provider_reports(llm_server):
    client = OpenAICompatibleLLMClient(llm_server["url"], "key", "m", timeout_seconds=5)
    llm_server["body"] = _chat('{"a": 1}', {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49})

    reply = client.complete([{"role": "user", "content": "x"}])

    assert (reply.content, reply.prompt_tokens, reply.completion_tokens) == ({"a": 1}, 42, 7)
    llm_server["body"] = _chat('{"a": 2}')
    assert client.complete([]).prompt_tokens is None and client.complete_json([]) == {"a": 2}


@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (503, True), (408, True), (400, False), (401, False), (404, False)])
def test_the_http_client_says_which_failures_are_worth_retrying(llm_server, status, retryable):
    client = OpenAICompatibleLLMClient(llm_server["url"], "key", "m", timeout_seconds=5)
    llm_server["status"], llm_server["body"] = status, {"error": "nope"}

    with pytest.raises(LLMError, match=f"LLM API error {status}") as raised:
        client.complete([])

    assert raised.value.status == status and raised.value.transient is retryable


def test_the_http_client_reads_how_long_the_provider_wants_us_to_wait(llm_server):
    client = OpenAICompatibleLLMClient(llm_server["url"], "key", "m", timeout_seconds=5)
    llm_server["status"] = 429
    llm_server["body"] = [{"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}}]

    with pytest.raises(LLMError) as from_body:
        client.complete([])
    llm_server["headers"], llm_server["body"] = {"Retry-After": "7"}, {"error": "slow down"}
    with pytest.raises(LLMError) as from_header:
        client.complete([])
    llm_server["headers"], llm_server["status"] = {}, 500
    with pytest.raises(LLMError) as unstated:
        client.complete([])

    assert from_body.value.retry_after == 41.0 and from_header.value.retry_after == 7.0 and unstated.value.retry_after is None


def test_a_malformed_reply_is_worth_asking_again_and_is_described_briefly(llm_server):
    client = OpenAICompatibleLLMClient(llm_server["url"], "key", "m", timeout_seconds=5)
    # What Gemini actually sent once: a junk character, a filter reason, and a large opaque blob.
    llm_server["body"] = {"choices": [{
        "finish_reason": "function_call_filter: MALFORMED_FUNCTION_CALL",
        "message": {"content": "ྀ", "extra_content": {"google": {"thought_signature": "E" * 5000}}},
    }]}

    with pytest.raises(LLMError, match="Invalid fixer payload") as raised:
        client.complete([], error_label="fixer")

    message = str(raised.value)
    assert raised.value.transient is True
    assert "MALFORMED_FUNCTION_CALL" in message and "thought_signature" not in message and len(message) < 300


def test_a_reply_with_an_unexpected_shape_is_described_without_dumping_it(llm_server):
    client = OpenAICompatibleLLMClient(llm_server["url"], "key", "m", timeout_seconds=5)
    llm_server["body"] = {"unexpected": "x" * 5000}

    with pytest.raises(LLMError, match="unexpected response shape") as raised:
        client.complete([])

    assert len(str(raised.value)) < 300


def test_a_long_provider_error_body_is_cut(llm_server):
    client = OpenAICompatibleLLMClient(llm_server["url"], "key", "m", timeout_seconds=5)
    llm_server["status"], llm_server["body"] = 400, {"error": "y" * 5000}

    with pytest.raises(LLMError) as raised:
        client.complete([])

    assert len(str(raised.value)) < 700


def test_the_router_asks_again_after_a_malformed_reply():
    malformed = LLMError("Invalid fixer payload: finish_reason='MALFORMED_FUNCTION_CALL'", transient=True)
    backend = FakeBackend(malformed, ok({"action": "final"}))
    router, _ = make_router({"main": backend}, {"default": ["main"]})

    assert router.client().complete_json([]) == {"action": "final"} and backend.calls == 2


def test_an_unreachable_endpoint_is_a_retryable_connection_error():
    client = OpenAICompatibleLLMClient("http://127.0.0.1:9", "key", "m", timeout_seconds=2)

    with pytest.raises(LLMError, match="LLM connection error") as raised:
        client.complete([])

    assert raised.value.transient


# --- inside a real agent run -----------------------------------------------------------------------------


def _fixer_router(*fixer_replies, backup=None, **kwargs):
    backends = {"cheap": (FakeBackend(*fixer_replies), (1.0, 2.0))}
    profiles = {"default": ["cheap"], "fixer.step": ["cheap"]}
    if backup is not None:
        backends["strong"] = (backup, (10.0, 20.0))
        profiles["fixer.step"] = ["cheap", "strong"]
    router, _ = make_router(backends, profiles, **kwargs)
    return router


def test_a_run_records_the_tokens_and_cost_of_its_model_calls_and_audits_them_without_content(tmp_path):
    h = build(tmp_path)
    router = _fixer_router(ok(edit(CONFIG_PATH, FIXED), 1000, 200), ok(done("Lowered latency_ms"), 1500, 50))
    h.runtime.fixer.llm = router.client("fixer.step")

    record = h.run()

    used = record.agent_run["budget"]
    assert record.agent_run_status == "ready_for_approval"
    assert used["llm_calls"] == 2 and used["tokens"] == 2750
    assert used["cost"] == pytest.approx((2500 * 1.0 + 250 * 2.0) / 1_000_000)
    calls = [e for e in h.audit.list(entity_id=h.change_id, limit=500) if e.action == "llm.call"]
    assert [(e.actor, e.details["profile"], e.details["endpoint"], e.details["ok"]) for e in calls] == [
        ("agent:fixer", "fixer.step", "cheap", True), ("agent:fixer", "fixer.step", "cheap", True),
    ]
    assert calls[0].details["prompt_tokens"] == 1000 and "latency_ms" in calls[0].details
    logged = json.dumps([e.details for e in calls])
    assert "error_probability" not in logged and "Lowered latency_ms" not in logged  # nothing the model said or was sent
    assert not any("messages" in e.details or "content" in e.details for e in calls)


def test_the_token_limit_stops_a_run_and_says_so(tmp_path):
    h = build(tmp_path)
    h.runtime.budget_limits = BudgetLimits(max_tokens=1000)
    h.runtime.fixer.llm = _fixer_router(ok(edit(CONFIG_PATH, FIXED), 900, 200), ok(done())).client("fixer.step")

    record = h.run()

    assert record.agent_run_status == "escalated" and "the run used its 1000 model tokens" in record.agent_run["escalation_reason"]
    assert record.agent_run["budget"]["tokens"] == 1100 and record.agent_run["budget"]["llm_calls"] == 1


def test_the_spending_limit_stops_a_run_and_says_so(tmp_path):
    h = build(tmp_path)
    h.runtime.budget_limits = BudgetLimits(max_cost=0.001)
    h.runtime.fixer.llm = _fixer_router(ok(edit(CONFIG_PATH, FIXED), 2000, 0), ok(done())).client("fixer.step")

    record = h.run()

    assert record.agent_run_status == "escalated" and "model spending limit of 0.001" in record.agent_run["escalation_reason"]


def test_a_run_survives_its_first_choice_model_failing(tmp_path):
    h = build(tmp_path)
    backup = FakeBackend(ok(edit(CONFIG_PATH, FIXED)), ok(done()))
    h.runtime.fixer.llm = _fixer_router(fatal("LLM API error 401: key revoked"), backup=backup, breaker_threshold=1).client("fixer.step")

    record = h.run()

    assert record.agent_run_status == "ready_for_approval" and backup.calls == 2
    calls = [e.details for e in h.audit.list(entity_id=h.change_id, limit=500) if e.action == "llm.call"]
    assert [(c["endpoint"], c["ok"]) for c in calls] == [("cheap", False), ("strong", True), ("strong", True)]
    assert "401" in calls[0]["error"]


def test_a_second_attempt_at_a_fix_goes_to_the_stronger_model(tmp_path):
    h = build(tmp_path)
    cheap = FakeBackend(ok(edit(CONFIG_PATH, INVALID)), ok(done("broke the file")))
    strong = FakeBackend(ok(edit(CONFIG_PATH, FIXED)), ok(done("fixed it properly")))
    router, _ = make_router(
        {"cheap": cheap, "strong": strong}, {"default": ["cheap"], "fixer.step": ["cheap", "strong"]},
        escalations={"fixer.step": Escalation(attempt_gte=2, use="strong")},
    )
    h.runtime.fixer.llm = router.client("fixer.step")

    record = h.run()

    assert record.agent_run_status == "ready_for_approval" and len(record.agent_run["attempts"]) == 2
    assert cheap.calls == 2 and strong.calls == 2  # attempt 1 on the cheap model, attempt 2 on the strong one


def test_the_graph_hands_the_investigator_the_runs_model_gate(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    gates = []
    original = h.runtime.investigator.investigate

    def spy(ctx, tools, llm_gate=None):
        gates.append(llm_gate)
        return original(ctx, tools, llm_gate=llm_gate)

    h.runtime.investigator.investigate = spy
    h.run()

    assert len(gates) == 1 and callable(gates[0])


def test_an_investigator_with_no_model_budget_left_answers_from_the_evidence(tmp_path):
    h = build(tmp_path, with_fixer_llm=False)
    asks_for_more = {"action": "tool", "tool": "logs", "method": "search", "args": {"query": "timeout"}}
    model = FakeBackend(ok(asks_for_more, prompt=900, completion=200))
    router, _ = make_router({"main": model}, {"default": ["main"]})
    h.runtime.investigator.llm = router.client()
    h.runtime.budget_limits = BudgetLimits(max_tokens=1000)  # its first call uses 1100

    record = h.run()

    investigation = record.agent_run["investigation"]
    assert model.calls == 1 and investigation["mode_used"] == "heuristic"
    assert investigation["hypotheses"][0]["issue_type"] == "performance_degradation"
    assert any("model tokens" in warning for warning in investigation["warnings"])


def test_a_critic_with_no_model_budget_left_reviews_by_the_rules(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    model = FakeBackend(ok({"concerns": [{"severity": "major", "text": "I would block this"}]}))
    router, _ = make_router({"main": model}, {"default": ["main"]})
    h.runtime.critic.llm = router.client()
    h.runtime.budget_limits = BudgetLimits(max_llm_calls=2)  # the Fixer's two steps use it all

    record = h.run()

    review = record.agent_run["review"]
    assert model.calls == 0 and review["mode_used"] == "rules" and review["decision"] == "approve_for_human"
    assert any("2 model calls" in warning for warning in review["warnings"])


# --- the timeline and the status endpoint ---------------------------------------------------------------


def test_model_calls_are_folded_into_one_timeline_line_with_their_tokens():
    def entry(actor, action, **details):
        return new_audit_entry(actor=actor, action=action, entity_id="CHG-1", entity_type="incident", details=details)

    timeline = build_timeline([
        entry("agent:fixer", "tool.sandbox.read", ok=True),
        entry("agent:fixer", "llm.call", model="big", prompt_tokens=100, completion_tokens=20, cost=0.002, ok=True),
        entry("agent:fixer", "llm.call", model="big", prompt_tokens=50, completion_tokens=5, cost=0.001, ok=False),
        entry("agent:fixer", "tool.sandbox.edit", ok=True),
    ])

    assert [(item["kind"], item["label"]) for item in timeline] == [
        ("tools", "used 1 tool call"), ("model", "made 2 model calls"), ("tools", "used 1 tool call"),
    ]
    assert timeline[1]["note"] == "big x2; 175 tokens; cost 0.0030" and timeline[1]["problem"] == "1 failed"


def test_the_status_endpoint_shows_routing_and_health_and_never_a_key(make_app, tmp_path):
    _, plain = make_app()
    assert plain.get("/v1/llm/status", headers=VIEWER).json() == {"configured": False}
    assert plain.get("/v1/llm/status").status_code == 401

    _, client = make_app(LLM_ROUTING_FILE=str(_routing_file(tmp_path)), KEY_A="very-secret", FIXER_MODE="llm", ALLOW_HOST_EXECUTION="true")
    status = client.get("/v1/llm/status", headers=VIEWER)

    body = status.json()
    assert body["configured"] and body["profiles"]["fixer.step"] == ["local", "strong"]
    assert body["endpoints"]["strong"] == {"model": "big", "local": False, "circuit": "closed"}
    assert "very-secret" not in status.text and "KEY_A" not in status.text
    assert client.post("/v1/incidents/mock", headers=APPROVER).status_code == 200  # the app still works end to end


# --- what a real model actually sends (seen with Gemini flash-lite) ---------------------------------------


@pytest.mark.parametrize(
    "content,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('---\n{"action": "final"}', {"action": "final"}),
        ('```json\n{"a": 3}\n```', {"a": 3}),
        ('Here you go: {"a": 4} hope that helps', {"a": 4}),
        ('{"a": 5} {"b": 6}', {"a": 5}),
    ],
)
def test_the_json_object_is_found_inside_what_models_wrap_around_it(content, expected):
    from app.adapters.llm_client import parse_json_object

    assert parse_json_object(content) == expected


@pytest.mark.parametrize("content", ["no json here", "[1, 2]", "{broken", ""])
def test_a_reply_with_no_usable_object_is_still_refused(content):
    from app.adapters.llm_client import parse_json_object

    with pytest.raises(ValueError):
        parse_json_object(content)


def test_tool_arguments_are_accepted_inside_args_or_beside_the_tool_name():
    from app.agents.models import tool_args

    assert tool_args({"action": "tool", "tool": "sandbox", "method": "grep", "args": {"pattern": "x"}}) == {"pattern": "x"}
    assert tool_args({"action": "tool", "tool": "sandbox", "method": "grep", "pattern": "x", "subdir": ""}) == {"pattern": "x", "subdir": ""}
    assert tool_args({"action": "tool", "tool": "sandbox", "method": "diff"}) == {}


def test_a_fixer_whose_model_flattens_its_arguments_still_makes_the_fix(tmp_path):
    flattened = [
        {"action": "tool", "tool": "sandbox", "method": "read", "path": CONFIG_PATH},
        {"action": "tool", "tool": "sandbox", "method": "edit", "path": CONFIG_PATH, "content": FIXED},
        done("Lowered latency_ms"),
    ]
    h = build(tmp_path, flattened)

    record = h.run()

    assert record.agent_run_status == "ready_for_approval" and record.agent_run["changed_files"] == [CONFIG_PATH]


def test_the_prompts_ask_for_the_bare_object_with_arguments_inside_args():
    from app.agents.prompts import CRITIC_SYSTEM_PROMPT, FIXER_SYSTEM_PROMPT, INVESTIGATOR_SYSTEM_PROMPT

    for prompt in (FIXER_SYSTEM_PROMPT, INVESTIGATOR_SYSTEM_PROMPT):
        assert "no text, separator or code\nfence before or after it" in prompt and 'go inside "args"' in prompt
    assert "nothing before or after it" in CRITIC_SYSTEM_PROMPT
