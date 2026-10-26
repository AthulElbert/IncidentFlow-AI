"""Replay known incident scenarios through the Investigator and score the result.

The scenarios are served by the mock tools, so a run is deterministic and needs no network.
The keyword classifier that the pipeline used before the Investigator is scored on the same
alerts as a baseline.
"""
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.agents.investigator import Investigator
from app.agents.models import IncidentContext, InvestigationReport
from app.audit import AuditLog
from app.models.schemas import APMEvent, IncidentRecord
from app.services.classifier import classify_issue
from app.tools.deploy_target import MockDeployTarget
from app.tools.deploys import MockDeployHistoryTool
from app.tools.instrumentation import Toolkit
from app.tools.logs import MockLogsTool
from app.tools.memory import LexicalIncidentMemory
from app.tools.metrics import MockMetricsTool
from app.tools.scenarios import DEFAULT_SCENARIOS, Scenario, eval_history


def toolkit_for(scenarios: tuple[Scenario, ...] | list[Scenario], history: list[IncidentRecord] | None) -> Toolkit:
    return Toolkit(
        metrics=MockMetricsTool(scenarios),
        logs=MockLogsTool(scenarios),
        deploys=MockDeployHistoryTool(scenarios),
        memory=LexicalIncidentMemory(lambda: list(history or [])),
        deploy_target=MockDeployTarget(),
    )


def event_for(scenario: Scenario) -> APMEvent:
    shape = scenario.metrics[scenario.alert_metric]
    return APMEvent(
        service=scenario.service,
        metric=scenario.alert_metric,
        value=shape.peak,
        threshold=scenario.alert_threshold,
        environment=scenario.environment,
        timestamp=scenario.anchor,
        message=scenario.alert_message,
    )


@dataclass
class ScenarioResult:
    scenario: str
    expected_issue_type: str
    predicted_issue_type: str
    correct: bool
    confidence: float
    decision: str
    expect_escalation: bool
    outcome: str  # see _outcome()
    baseline_issue_type: str
    baseline_correct: bool
    keyword_hit_rate: float | None
    tool_calls: int
    mode_used: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReplaySummary:
    results: list[ScenarioResult]

    @property
    def total(self) -> int:
        return len(self.results)

    def _rate(self, flags: list[bool]) -> float:
        return round(sum(flags) / len(flags), 3) if flags else 0.0

    @property
    def accuracy(self) -> float:
        return self._rate([r.correct for r in self.results])

    @property
    def baseline_accuracy(self) -> float:
        return self._rate([r.baseline_correct for r in self.results])

    @property
    def harmful(self) -> int:
        """Proceeded when it should not have: with a wrong answer, or where only a human could help.

        This is the failure the confidence gate exists to prevent.
        """
        return sum(1 for r in self.results if r.outcome in HARMFUL_OUTCOMES)

    @property
    def automation_rate(self) -> float:
        """Share of answerable incidents the Investigator resolved without a human, correctly."""
        answerable = [r for r in self.results if not r.expect_escalation]
        return self._rate([r.outcome == "automated_correctly" for r in answerable])

    @property
    def unneeded_escalations(self) -> int:
        """Escalated although an answerable incident: the cost of caution (not a harm)."""
        return sum(1 for r in self.results if r.outcome in {"escalated_with_correct_top", "escalated_wrong_top"})

    @property
    def root_cause_hit_rate(self) -> float:
        rates = [r.keyword_hit_rate for r in self.results if r.keyword_hit_rate is not None]
        return round(sum(rates) / len(rates), 3) if rates else 0.0

    def _mean_confidence(self, correct: bool) -> float | None:
        values = [r.confidence for r in self.results if r.correct == correct]
        return round(sum(values) / len(values), 3) if values else None

    def to_dict(self) -> dict:
        return {
            "scenarios": self.total,
            "accuracy": self.accuracy,
            "baseline_accuracy": self.baseline_accuracy,
            "automation_rate": self.automation_rate,
            "harmful": self.harmful,
            "unneeded_escalations": self.unneeded_escalations,
            "root_cause_hit_rate": self.root_cause_hit_rate,
            "mean_confidence_when_correct": self._mean_confidence(True),
            "mean_confidence_when_wrong": self._mean_confidence(False),
            "results": [asdict(r) for r in self.results],
        }


HARMFUL_OUTCOMES = frozenset({"proceeded_wrong", "proceeded_unsafely"})


def _outcome(scenario: Scenario, report: InvestigationReport, correct: bool) -> str:
    proceeded = report.decision == "proceed"
    if scenario.expect_escalation:
        return "proceeded_unsafely" if proceeded else "escalated_as_expected"
    if proceeded:
        return "automated_correctly" if correct else "proceeded_wrong"
    return "escalated_with_correct_top" if correct else "escalated_wrong_top"


def _keyword_hit_rate(scenario: Scenario, report: InvestigationReport) -> float | None:
    if not scenario.root_cause_keywords or report.top is None:
        return None
    text = report.top.root_cause.lower()
    hits = sum(1 for word in scenario.root_cause_keywords if word.lower() in text)
    return round(hits / len(scenario.root_cause_keywords), 3)


def run_replay(
    investigator: Investigator,
    scenarios: tuple[Scenario, ...] | list[Scenario] = DEFAULT_SCENARIOS,
    history: list[IncidentRecord] | None = None,
    audit: AuditLog | None = None,
    on_result: Callable[[ScenarioResult], None] | None = None,
) -> ReplaySummary:
    history = eval_history() if history is None else history
    toolkit = toolkit_for(scenarios, history)
    results: list[ScenarioResult] = []

    for scenario in scenarios:
        event = event_for(scenario)
        ctx = IncidentContext.from_event(f"REPLAY-{scenario.name}", event)
        tools = toolkit.for_agent("investigator", entity_id=ctx.incident_id, audit=audit)
        report = investigator.investigate(ctx, tools)

        top = report.top
        predicted = top.issue_type if top else "unknown"
        correct = predicted == scenario.expected_issue_type
        baseline = classify_issue(event)[0]

        result = ScenarioResult(
            scenario=scenario.name,
            expected_issue_type=scenario.expected_issue_type,
            predicted_issue_type=predicted,
            correct=correct,
            confidence=top.confidence if top else 0.0,
            decision=report.decision,
            expect_escalation=scenario.expect_escalation,
            outcome=_outcome(scenario, report, correct),
            baseline_issue_type=baseline,
            baseline_correct=baseline == scenario.expected_issue_type,
            keyword_hit_rate=_keyword_hit_rate(scenario, report),
            tool_calls=report.tool_calls_used,
            mode_used=report.mode_used,
            warnings=report.warnings,
        )
        results.append(result)
        if on_result:
            on_result(result)

    return ReplaySummary(results)
