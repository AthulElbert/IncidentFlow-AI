"""A simulated dev environment: one object that is both the deploy target and its metrics.

Deploying a branch changes what the metrics report afterwards, according to the effect that was
registered for that branch. The Verifier is not told the effect; it has to find out from the
numbers, which is what makes this useful for testing it. A real backend (a dev cluster with its
own Prometheus) replaces this behind the same two interfaces.
"""
import random
import threading
from datetime import timedelta

from app.tools.base import TimeWindow, ToolError
from app.tools.models import DeployResult, MetricPoint, MetricSeries
from app.tools.scenarios import DEFAULT_SCENARIOS, Scenario, scenario_for

EFFECTS = ("resolves", "no_effect", "partial", "regresses_other", "deploy_fails")
_PARTIAL_REMAINING = 0.6  # a partial fix leaves 60% of the original problem in place


class MockDevEnvironment:
    def __init__(
        self,
        scenarios: tuple[Scenario, ...] | list[Scenario] = DEFAULT_SCENARIOS,
        default_effect: str = "resolves",
        polls_until_done: int = 1,
    ) -> None:
        if default_effect not in EFFECTS:
            raise ValueError(f"unknown dev effect '{default_effect}'; expected one of {EFFECTS}")
        self.scenarios = tuple(scenarios)
        self.default_effect = default_effect
        self.polls_until_done = max(0, polls_until_done)
        self._effects: dict[str, str] = {}
        self._deploys: dict[str, tuple[str, str, str]] = {}  # url -> (service, ref, effect)
        self._polls: dict[str, int] = {}
        self._active: dict[str, str] = {}  # service -> effect of the last successful dev deploy
        self._counter = 0
        self._lock = threading.Lock()
        self.deploy_target = _EnvDeployTarget(self)
        self.metrics = _EnvMetrics(self)

    def register_fix(self, ref: str, effect: str) -> None:
        """Declare what deploying `ref` will do. Test and demo setup only; the Verifier never sees this."""
        if effect not in EFFECTS:
            raise ValueError(f"unknown dev effect '{effect}'; expected one of {EFFECTS}")
        self._effects[ref] = effect

    def deployed(self, service: str) -> str | None:
        return self._active.get(service)

    def reset(self) -> None:
        with self._lock:
            self._deploys.clear()
            self._polls.clear()
            self._active.clear()


class _EnvDeployTarget:
    def __init__(self, env: MockDevEnvironment) -> None:
        self._env = env

    def deploy_dev(self, service: str, ref: str, issue_type: str = "unknown") -> DeployResult:
        env = self._env
        with env._lock:
            env._counter += 1
            url = f"mock://dev/{service}/{env._counter}"
            env._deploys[url] = (service, ref, env._effects.get(ref, env.default_effect))
        return DeployResult(status="QUEUED", url=url, detail=f"dev deploy of {ref}")

    def get_status(self, deploy_url: str) -> DeployResult:
        env = self._env
        with env._lock:
            if deploy_url not in env._deploys:
                raise ToolError(f"unknown dev deploy {deploy_url}")
            service, _, effect = env._deploys[deploy_url]
            polls = env._polls.get(deploy_url, 0) + 1
            env._polls[deploy_url] = polls
            if polls <= env.polls_until_done:
                return DeployResult(status="RUNNING", url=deploy_url)
            if effect == "deploy_fails":
                return DeployResult(status="FAILURE", url=deploy_url, detail="build failed")
            env._active[service] = effect
            return DeployResult(status="SUCCESS", url=deploy_url)

    def canary(self, service: str, ref: str, percent: int) -> DeployResult:
        raise ToolError("canary is not available in the dev environment simulator")

    def rollback(self, service: str, ref: str) -> DeployResult:
        raise ToolError("rollback is not available in the dev environment simulator")


class _EnvMetrics:
    def __init__(self, env: MockDevEnvironment) -> None:
        self._env = env

    def available_metrics(self, service: str) -> list[str]:
        scenario = scenario_for(service, self._env.scenarios)
        return sorted(scenario.metrics) if scenario else []

    def query(self, service: str, metric: str, window: TimeWindow, step_seconds: int = 60) -> MetricSeries:
        scenario = scenario_for(service, self._env.scenarios)
        if scenario is None or metric not in scenario.metrics:
            raise ToolError(f"no dev metric '{metric}' for {service}")
        shape = scenario.metrics[metric]
        effect = self._env.deployed(service)
        is_alert_metric = metric == scenario.alert_metric

        if effect is None or effect == "no_effect":
            level = shape.peak  # nothing has changed: the problem is still there
        elif effect == "resolves":
            level = shape.baseline
        elif effect == "partial":
            level = shape.baseline + _PARTIAL_REMAINING * (shape.peak - shape.baseline) if is_alert_metric else shape.baseline
        else:  # regresses_other: the alert metric recovers but everything else gets worse
            level = shape.baseline if is_alert_metric else shape.peak

        points: list[MetricPoint] = []
        step = max(1, step_seconds)
        moment = window.start
        while moment <= window.end:
            jitter = random.Random(f"dev:{scenario.name}:{metric}:{effect}:{int(moment.timestamp()) // step}").uniform(-1, 1)
            points.append(MetricPoint(timestamp=moment, value=round(max(0.0, level * (1 + shape.noise * jitter)), 4)))
            moment += timedelta(seconds=step)
        return MetricSeries(service=service, metric=metric, unit=shape.unit, points=points)
