from datetime import datetime, timezone

from app.models.schemas import APMEvent
from app.services.triage_agent import TriageAgent


class GoodLLM:
    def triage(self, event: APMEvent) -> dict:
        return {
            "issue_type": "dependency_failure",
            "confidence": 0.83,
            "probable_cause": "DB connection pool saturation",
            "hypothesis_steps": [
                "Connection timeout pattern increased",
                "DB wait time correlated with error spike",
            ],
        }


class BadLLM:
    def triage(self, event: APMEvent) -> dict:
        return {"bad": "shape"}


class TimeoutLLM:
    def triage(self, event: APMEvent) -> dict:
        raise RuntimeError("timeout")



def _event() -> APMEvent:
    return APMEvent(
        service="payments-service",
        metric="db_timeout_count",
        value=120,
        threshold=20,
        environment="prod",
        timestamp=datetime.now(timezone.utc),
        message="Connection timeout surge",
    )


def test_triage_llm_success():
    agent = TriageAgent(mode="llm", llm_client=GoodLLM(), confidence_floor=0.60)
    out = agent.triage(_event())

    assert out.mode_used == "llm"
    assert out.issue_type == "dependency_failure"
    assert out.confidence == 0.83
    assert len(out.hypothesis_steps) == 2
    assert out.warnings == []


def test_triage_llm_bad_payload_fallbacks_to_heuristic():
    agent = TriageAgent(mode="llm", llm_client=BadLLM(), confidence_floor=0.60)
    out = agent.triage(_event())

    assert out.mode_used == "heuristic"
    assert out.issue_type in {
        "performance_degradation",
        "application_error",
        "resource_saturation",
        "dependency_failure",
        "unknown",
    }
    assert len(out.warnings) == 1
    assert "LLM triage fallback" in out.warnings[0]


def test_triage_llm_timeout_fallbacks_to_heuristic():
    agent = TriageAgent(mode="llm", llm_client=TimeoutLLM(), confidence_floor=0.60)
    out = agent.triage(_event())

    assert out.mode_used == "heuristic"
    assert len(out.warnings) == 1
    assert "LLM triage fallback" in out.warnings[0]
