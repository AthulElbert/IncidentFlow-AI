"""Routes each model call to an endpoint chosen by the task it is for, and survives an endpoint failing.

A *profile* names a task (`fixer.step`, `critic.review`, ...). Configuration maps each profile to an ordered list
of endpoints. A call tries them in order: a transient error (timeout, 429, 5xx) is retried on the same endpoint
with backoff, any other error moves on to the next endpoint, and an endpoint that keeps failing is skipped for
a cooldown (a circuit breaker). Every physical call, retries included, is reported with its token usage and cost.

When the provider says how long to wait (a rate limit's Retry-After or retryDelay), that wait is used instead of
the backoff, up to `max_retry_after_seconds`. Such a response means "too many requests", not "this endpoint is
broken", so it does not count towards the circuit breaker. A longer requested wait is not sat out: the call moves
on to the next endpoint, or fails so the agent can fall back.

Which model handles a task is decided by configuration and by facts the code knows (a retry of a failed fix can
use a stronger model). No model is asked how hard its own task is.
"""
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from app.adapters.llm_client import (
    LLMError,
    LLMReply,
    OpenAICompatibleLLMClient,
    code_fix_messages,
    triage_messages,
)
from app.adapters.llm_usage import LLMCall, current_llm_context, report_llm_call
from app.models.schemas import APMEvent

logger = logging.getLogger("app.llm_router")

DEFAULT_PROFILE = "default"
KNOWN_PROFILES = frozenset(
    {DEFAULT_PROFILE, "triage", "code_fix", "investigator.reason", "fixer.step", "critic.review", "embedding"}
)


class RoutingConfigError(ValueError):
    pass


class CompletionBackend(Protocol):
    def complete(self, messages: list[dict], error_label: str = "LLM") -> LLMReply:
        ...


@dataclass
class Endpoint:
    name: str
    client: CompletionBackend
    model: str
    price_in: float = 0.0  # per million prompt tokens
    price_out: float = 0.0  # per million completion tokens
    local: bool = False  # the model runs inside our network; data sent to it does not leave

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round((prompt_tokens * self.price_in + completion_tokens * self.price_out) / 1_000_000, 6)


class CircuitBreaker:
    """Stops sending calls to an endpoint that keeps failing, then lets one through after a cooldown."""

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = max(1, threshold)
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            return "half-open" if self._clock() - self._opened_at >= self.cooldown_seconds else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = self._clock()  # a failed trial after the cooldown re-opens it


@dataclass
class Escalation:
    attempt_gte: int
    use: str


class ProfiledLLM:
    """The router seen from one task: an `LLMClient` whose calls all carry that task's profile."""

    def __init__(self, router: "LLMRouter", profile: str) -> None:
        self.router = router
        self.profile = profile

    @property
    def model(self) -> str:
        """The model this task normally uses (its first choice)."""
        return self.router.primary(self.profile).model

    def complete_json(self, messages: list[dict], error_label: str = "LLM") -> dict:
        return self.router.complete(self.profile, messages, error_label)

    def triage(self, event: APMEvent) -> dict:
        return self.complete_json(triage_messages(event), error_label="LLM triage")

    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return self.complete_json(code_fix_messages(file_path, file_content, instructions), error_label="LLM code-fix")


@dataclass
class LLMRouter:
    endpoints: dict[str, Endpoint]
    profiles: dict[str, list[str]]
    escalations: dict[str, Escalation] = field(default_factory=dict)
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    breaker_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0
    max_retry_after_seconds: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        _check_routing(self.endpoints, self.profiles, self.escalations)
        self._breakers = {
            name: CircuitBreaker(self.breaker_threshold, self.breaker_cooldown_seconds, self.clock) for name in self.endpoints
        }
        self._clients: dict[str, ProfiledLLM] = {}

    # ------------------------------------------------------------------ lookups

    def has_profile(self, profile: str) -> bool:
        return profile in self.profiles

    def client(self, profile: str = DEFAULT_PROFILE) -> ProfiledLLM:
        """The client for a task. A profile with no routing of its own uses the default one."""
        name = profile if profile in self.profiles else DEFAULT_PROFILE
        if name not in self._clients:
            self._clients[name] = ProfiledLLM(self, name)
        return self._clients[name]

    def primary(self, profile: str) -> Endpoint:
        return self.endpoints[self.profiles.get(profile, self.profiles[DEFAULT_PROFILE])[0]]

    def chain(self, profile: str, attempt: int = 1) -> list[Endpoint]:
        names = list(self.profiles.get(profile, self.profiles[DEFAULT_PROFILE]))
        rule = self.escalations.get(profile)
        if rule and attempt >= rule.attempt_gte:
            names = [rule.use] + [name for name in names if name != rule.use]
        return [self.endpoints[name] for name in names]

    def status(self) -> dict[str, Any]:
        """Routing and endpoint health, with nothing secret in it."""
        return {
            "profiles": {name: list(chain) for name, chain in sorted(self.profiles.items())},
            "endpoints": {
                name: {"model": endpoint.model, "local": endpoint.local, "circuit": self._breakers[name].state}
                for name, endpoint in sorted(self.endpoints.items())
            },
            "escalate_on_retry": {name: {"attempt_gte": r.attempt_gte, "use": r.use} for name, r in self.escalations.items()},
        }

    # --------------------------------------------------------------------- calls

    def complete(self, profile: str, messages: list[dict], error_label: str = "LLM") -> dict:
        chain = self.chain(profile, current_llm_context().attempt)
        failures: list[str] = []
        last_error: Exception | None = None

        for endpoint in chain:
            breaker = self._breakers[endpoint.name]
            if not breaker.allow():
                failures.append(f"{endpoint.name}: skipped, it has been failing")
                continue
            reply, error = self._call_endpoint(endpoint, breaker, profile, messages, error_label)
            if reply is not None:
                return reply.content
            last_error = error
            failures.append(f"{endpoint.name}: {str(error)[:200]}")

        if last_error is not None and len(chain) == 1:
            raise last_error  # one endpoint, one cause: keep its own message
        detail = "; ".join(failures)
        if last_error is None:
            raise LLMError(f"{error_label}: no model endpoint is available ({detail})", transient=True)
        raise LLMError(f"{error_label}: every model endpoint failed ({detail})") from last_error

    def _call_endpoint(
        self, endpoint: Endpoint, breaker: CircuitBreaker, profile: str, messages: list[dict], error_label: str
    ) -> tuple[LLMReply | None, Exception | None]:
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            started = self.clock()
            try:
                reply = endpoint.client.complete(messages, error_label)
            except Exception as exc:  # LLMError for anything the client understood; others are treated as final
                error = exc
                self._report(endpoint, profile, started, ok=False, error=str(exc))
                transient = isinstance(exc, LLMError) and exc.transient
                asked_wait = exc.retry_after if isinstance(exc, LLMError) and transient else None
                if asked_wait is None:
                    breaker.failure()  # a rate limit with a stated wait is not a broken endpoint
                if not transient or attempt >= self.max_retries or not breaker.allow():
                    break
                if asked_wait is not None and asked_wait > self.max_retry_after_seconds:
                    break  # too long to sit out inside an agent run; try the next endpoint or give up
                self.sleep(asked_wait if asked_wait is not None else self._backoff(attempt))
                continue
            breaker.success()
            self._report(endpoint, profile, started, ok=True, reply=reply)
            return reply, None
        return None, error

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.backoff_max_seconds, self.backoff_base_seconds * (2**attempt))
        return ceiling * (0.5 + self.jitter() / 2)  # jitter, so many clients do not retry in step

    def _report(self, endpoint: Endpoint, profile: str, started: float, ok: bool, reply: LLMReply | None = None, error: str = "") -> None:
        prompt = (reply.prompt_tokens if reply else None) or 0
        completion = (reply.completion_tokens if reply else None) or 0
        call = LLMCall(
            profile=profile,
            endpoint=endpoint.name,
            model=endpoint.model,
            ok=ok,
            latency_ms=round((self.clock() - started) * 1000, 1),
            prompt_tokens=prompt,
            completion_tokens=completion,
            usage_reported=bool(reply and reply.prompt_tokens is not None),
            cost=endpoint.cost(prompt, completion),
            error=error[:200],
        )
        try:
            report_llm_call(call)
        except Exception:  # accounting must never break the call it is accounting for
            logger.exception("llm-usage-report-failed", extra={"profile": profile, "endpoint": endpoint.name})


# ------------------------------------------------------------------------------ configuration


def _check_routing(endpoints: Mapping[str, Endpoint], profiles: Mapping[str, list[str]], escalations: Mapping[str, Escalation]) -> None:
    if not endpoints:
        raise RoutingConfigError("LLM routing needs at least one endpoint")
    if DEFAULT_PROFILE not in profiles:
        raise RoutingConfigError(f"LLM routing needs a '{DEFAULT_PROFILE}' profile")
    for profile, chain in profiles.items():
        if profile not in KNOWN_PROFILES:
            raise RoutingConfigError(f"unknown LLM profile '{profile}' (known: {', '.join(sorted(KNOWN_PROFILES))})")
        if not chain:
            raise RoutingConfigError(f"LLM profile '{profile}' lists no endpoints")
        for name in chain:
            if name not in endpoints:
                raise RoutingConfigError(f"LLM profile '{profile}' uses unknown endpoint '{name}'")
    for profile, rule in escalations.items():
        if profile not in profiles:
            raise RoutingConfigError(f"escalate_on_retry names unknown profile '{profile}'")
        if rule.use not in endpoints:
            raise RoutingConfigError(f"escalate_on_retry for '{profile}' uses unknown endpoint '{rule.use}'")
        if rule.attempt_gte < 2:
            raise RoutingConfigError(f"escalate_on_retry for '{profile}' needs attempt_gte of 2 or more")


def single_endpoint_router(
    base_url: str, api_key: str, model: str, timeout_seconds: int = 20, verify_ssl: bool = True, max_retries: int = 2
) -> LLMRouter:
    """Routing for the plain `LLM_*` settings: one endpoint that every task uses."""
    client = OpenAICompatibleLLMClient(base_url, api_key, model, timeout_seconds, verify_ssl)
    return LLMRouter({"default": Endpoint("default", client, model)}, {DEFAULT_PROFILE: ["default"]}, max_retries=max_retries)


def load_routing_file(path: str | Path, env: Mapping[str, str], max_retries: int = 2) -> LLMRouter:
    """Build a router from a JSON routing file. API keys are named by environment variable, never written in the file."""
    file = Path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RoutingConfigError(f"cannot read LLM routing file {file}: {exc}") from exc
    except ValueError as exc:
        raise RoutingConfigError(f"LLM routing file {file} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RoutingConfigError("LLM routing file must be a JSON object")

    endpoints: dict[str, Endpoint] = {}
    for name, spec in (raw.get("endpoints") or {}).items():
        if not isinstance(spec, dict):
            raise RoutingConfigError(f"endpoint '{name}' must be an object")
        local = bool(spec.get("local", False))
        key_env = str(spec.get("api_key_env", "")).strip()
        api_key = env.get(key_env, "").strip() if key_env else ""
        if not api_key and not local:
            raise RoutingConfigError(f"endpoint '{name}' needs api_key_env naming a set environment variable")
        prices = spec.get("price_per_mtok", [0, 0])
        if not (isinstance(prices, list) and len(prices) == 2 and all(isinstance(p, (int, float)) and p >= 0 for p in prices)):
            raise RoutingConfigError(f"endpoint '{name}': price_per_mtok must be [input, output], both 0 or more")
        try:
            client = OpenAICompatibleLLMClient(
                base_url=str(spec.get("base_url", "")).strip(),
                api_key=api_key or "local",  # local servers ignore the key but the header must be present
                model=str(spec.get("model", "")).strip(),
                timeout_seconds=int(spec.get("timeout_seconds", 20)),
                verify_ssl=bool(spec.get("verify_ssl", True)),
            )
        except ValueError as exc:
            raise RoutingConfigError(f"endpoint '{name}' needs base_url and model") from exc
        endpoints[name] = Endpoint(name, client, client.model, float(prices[0]), float(prices[1]), local)

    profiles = {str(name): [str(item) for item in chain] for name, chain in (raw.get("profiles") or {}).items() if isinstance(chain, list)}
    escalations: dict[str, Escalation] = {}
    for profile, rule in (raw.get("escalate_on_retry") or {}).items():
        if not isinstance(rule, dict) or "use" not in rule:
            raise RoutingConfigError(f"escalate_on_retry for '{profile}' needs 'attempt_gte' and 'use'")
        escalations[str(profile)] = Escalation(int(rule.get("attempt_gte", 2)), str(rule["use"]))

    router = LLMRouter(
        endpoints,
        profiles,
        escalations,
        max_retries=int(raw.get("max_retries", max_retries)),
        max_retry_after_seconds=float(raw.get("max_retry_after_seconds", 60.0)),
    )

    # A profile marked local-only may never send its data to an endpoint outside our network.
    for profile in raw.get("local_only_profiles") or []:
        if profile not in profiles:
            raise RoutingConfigError(f"local_only_profiles names unknown profile '{profile}'")
        outside = [e.name for e in router.chain(profile) if not e.local]
        rule = escalations.get(profile)
        if rule and not endpoints[rule.use].local:
            outside.append(rule.use)
        if outside:
            raise RoutingConfigError(f"profile '{profile}' is local-only but can reach non-local endpoint(s): {', '.join(sorted(set(outside)))}")
    return router
