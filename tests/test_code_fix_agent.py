from app.services.code_fix_agent import CodeFixAgent

_CURRENT_CONTENT = '{"latency_ms": 1800, "error_probability": 0.25}\n'


class GoodFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return {
            "fixed_content": '{"latency_ms": 150, "error_probability": 0.01}\n',
            "summary": "Reduced injected latency and error probability to safe production values.",
        }


class StillUnsafeFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return {
            "fixed_content": '{"latency_ms": 900, "error_probability": 0.2}\n',
            "summary": "Slightly reduced values.",
        }


class InvalidJsonFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return {"fixed_content": "not json at all", "summary": "oops"}


class MissingKeysFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return {"fixed_content": '{"unrelated": true}\n', "summary": "changed the wrong thing"}


class EmptyContentFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return {"fixed_content": "", "summary": ""}


class ExplodingFixLLM:
    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        raise RuntimeError("upstream timeout")


class ImprovesOnFeedbackFixLLM:
    """Fails the first attempt, then succeeds once it sees feedback -- exercises the retry loop."""

    def __init__(self) -> None:
        self.calls = 0

    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        self.calls += 1
        if "Feedback from your previous attempt" not in instructions:
            return {
                "fixed_content": '{"latency_ms": 900, "error_probability": 0.2}\n',
                "summary": "First pass: partial reduction.",
            }
        return {
            "fixed_content": '{"latency_ms": 150, "error_probability": 0.01}\n',
            "summary": "Second pass: applied the feedback and reduced further.",
        }


def test_code_fix_agent_skips_when_unconfigured():
    agent = CodeFixAgent(llm_client=None)
    assert agent.is_configured() is False

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)
    assert result.status == "skipped"


def test_code_fix_agent_applies_verified_fix():
    agent = CodeFixAgent(llm_client=GoodFixLLM(), max_latency_ms=300.0, max_error_probability=0.02)
    assert agent.is_configured() is True

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)
    assert result.status == "applied"
    assert "150" in result.fixed_content
    assert result.summary


def test_code_fix_agent_rejects_fix_that_is_still_unsafe():
    agent = CodeFixAgent(llm_client=StillUnsafeFixLLM(), max_latency_ms=300.0, max_error_probability=0.02)

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)
    assert result.status == "failed"
    assert "safety verification" in result.message


def test_code_fix_agent_rejects_invalid_json():
    agent = CodeFixAgent(llm_client=InvalidJsonFixLLM())

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)
    assert result.status == "failed"
    assert "not valid JSON" in result.message


def test_code_fix_agent_rejects_missing_keys():
    agent = CodeFixAgent(llm_client=MissingKeysFixLLM())

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)
    assert result.status == "failed"
    assert "missing expected keys" in result.message


def test_code_fix_agent_rejects_empty_content():
    agent = CodeFixAgent(llm_client=EmptyContentFixLLM())

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)
    assert result.status == "failed"
    assert "empty fixed_content" in result.message


def test_code_fix_agent_handles_llm_exception():
    agent = CodeFixAgent(llm_client=ExplodingFixLLM())

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)
    assert result.status == "failed"
    assert "upstream timeout" in result.message


def test_code_fix_agent_retries_with_feedback_and_succeeds():
    llm = ImprovesOnFeedbackFixLLM()
    agent = CodeFixAgent(llm_client=llm, max_latency_ms=300.0, max_error_probability=0.02, max_attempts=3)

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)

    assert result.status == "applied"
    assert llm.calls == 2
    assert "150" in result.fixed_content
    assert len(result.attempts) == 2
    assert result.attempts[0].verified is False
    assert result.attempts[1].verified is True
    assert "2 attempt(s)" in result.message


def test_code_fix_agent_gives_up_after_max_attempts():
    agent = CodeFixAgent(llm_client=StillUnsafeFixLLM(), max_latency_ms=300.0, max_error_probability=0.02, max_attempts=2)

    result = agent.propose_fix("faulty_project/app_config.json", _CURRENT_CONTENT)

    assert result.status == "failed"
    assert len(result.attempts) == 2
    assert "after 2 attempt(s)" in result.message
