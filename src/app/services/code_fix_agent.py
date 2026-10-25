import json
import random
from dataclasses import dataclass, field

from app.adapters.llm_client import LLMClient

_INSTRUCTIONS = (
    "This is a service configuration file with intentionally injected faults. "
    "Identify any fields that represent unsafe production values (e.g. an "
    "artificially high artificial-latency setting, or an artificially high "
    "error-injection probability) and correct them to safe, healthy production "
    "values. Keep the file's structure and any unrelated fields unchanged. "
    "Only change the values that are clearly faulty."
)


@dataclass(frozen=True)
class CodeFixAttempt:
    attempt: int
    verified: bool
    reason: str
    simulated_p99_latency_ms: float
    simulated_error_rate: float


@dataclass(frozen=True)
class CodeFixResult:
    status: str  # "applied" | "skipped" | "failed"
    message: str
    fixed_content: str = ""
    summary: str = ""
    test_output: str = ""
    attempts: list[CodeFixAttempt] = field(default_factory=list)


class CodeFixAgent:
    """Iteratively investigates a single known target file and fixes it.

    Scope is intentionally narrow: this targets faulty_project/app_config.json's
    two known fault fields (latency_ms, error_probability), not open-ended
    repo-wide debugging. Each loop iteration is: Gemini proposes a fix -> a
    deterministic check verifies it (backed by a simulation of the real
    checkout behavior in faulty_project/app.py) -> on failure, the simulated
    results are fed back to Gemini for the next attempt, up to max_attempts.
    """

    def __init__(
        self,
        llm_client: LLMClient | None,
        max_latency_ms: float = 300.0,
        max_error_probability: float = 0.02,
        max_attempts: int = 3,
    ) -> None:
        self.llm_client = llm_client
        self.max_latency_ms = max_latency_ms
        self.max_error_probability = max_error_probability
        self.max_attempts = max(1, max_attempts)

    def is_configured(self) -> bool:
        return self.llm_client is not None

    def propose_fix(self, file_path: str, current_content: str) -> CodeFixResult:
        if self.llm_client is None:
            return CodeFixResult(status="skipped", message="no LLM client configured for code-fix")

        attempts: list[CodeFixAttempt] = []
        feedback: str | None = None
        last_fixed_content = ""
        last_summary = ""

        for attempt_num in range(1, self.max_attempts + 1):
            try:
                raw = self.llm_client.propose_code_fix(
                    file_path=file_path,
                    file_content=current_content,
                    instructions=self._instructions(feedback),
                )
            except Exception as exc:
                attempts.append(CodeFixAttempt(attempt_num, False, f"LLM call failed: {exc}", 0.0, 0.0))
                feedback = f"Your previous attempt raised an error: {exc}. Please return valid JSON this time."
                continue

            fixed_content = str(raw.get("fixed_content", "")).strip()
            summary = str(raw.get("summary", "")).strip() or "No summary provided."
            last_fixed_content, last_summary = fixed_content, summary

            if not fixed_content:
                attempts.append(CodeFixAttempt(attempt_num, False, "empty fixed_content", 0.0, 0.0))
                feedback = "Your fixed_content was empty. Return the complete corrected file content."
                continue

            ok, reason, p99, err_rate = self._verify_and_simulate(fixed_content)
            attempts.append(CodeFixAttempt(attempt_num, ok, reason, p99, err_rate))

            if ok:
                return CodeFixResult(
                    status="applied",
                    message=f"LLM fix verified after {attempt_num} attempt(s)",
                    fixed_content=fixed_content,
                    summary=summary,
                    test_output=self._format_test_output(p99, err_rate, passed=True),
                    attempts=attempts,
                )

            feedback = (
                f"Your previous fix (attempt {attempt_num}) was rejected: {reason}. "
                f"Simulated result: p99 latency={p99:.0f}ms, error_rate={err_rate:.1%}. "
                f"Propose a corrected version where latency_ms produces p99 latency <= "
                f"{self.max_latency_ms:.0f}ms and error_probability <= {self.max_error_probability:.1%}."
            )

        last = attempts[-1] if attempts else None
        test_output = (
            self._format_test_output(last.simulated_p99_latency_ms, last.simulated_error_rate, passed=False)
            if last
            else ""
        )
        reason = last.reason if last else "no attempts recorded"
        return CodeFixResult(
            status="failed",
            message=f"LLM fix rejected by safety verification after {len(attempts)} attempt(s): {reason}",
            fixed_content=last_fixed_content,
            summary=last_summary,
            test_output=test_output,
            attempts=attempts,
        )

    def _instructions(self, feedback: str | None) -> str:
        if not feedback:
            return _INSTRUCTIONS
        return f"{_INSTRUCTIONS}\n\nFeedback from your previous attempt, which was rejected:\n{feedback}"

    def _verify_and_simulate(self, fixed_content: str) -> tuple[bool, str, float, float]:
        try:
            data = json.loads(fixed_content)
        except Exception as exc:
            return False, f"fixed content is not valid JSON: {exc}", 0.0, 0.0

        if not isinstance(data, dict):
            return False, "fixed content JSON must be an object", 0.0, 0.0

        if "latency_ms" not in data or "error_probability" not in data:
            return False, "missing expected keys (latency_ms, error_probability)", 0.0, 0.0

        try:
            latency_ms = float(data["latency_ms"])
            error_probability = float(data["error_probability"])
        except Exception:
            return False, "latency_ms/error_probability must be numeric", 0.0, 0.0

        if latency_ms < 0 or error_probability < 0:
            return False, "negative values are invalid", latency_ms, error_probability

        p99, err_rate = self._simulate(latency_ms, error_probability)

        if p99 > self.max_latency_ms:
            return False, f"simulated p99 latency {p99:.0f}ms still above safe bound {self.max_latency_ms:.0f}ms", p99, err_rate

        if err_rate > self.max_error_probability:
            return False, f"simulated error rate {err_rate:.1%} still above safe bound {self.max_error_probability:.1%}", p99, err_rate

        return True, "ok", p99, err_rate

    def _simulate(self, latency_ms: float, error_probability: float, trials: int = 500) -> tuple[float, float]:
        """Mirrors faulty_project/app.py's /api/checkout logic without running a server:
        delay ~ uniform(0.7x, 1.3x) of latency_ms, error with probability error_probability.
        """
        rng = random.Random(1234)
        delays = sorted(rng.uniform(latency_ms * 0.7, latency_ms * 1.3) for _ in range(trials))
        p99_index = min(trials - 1, int(trials * 0.99))
        p99 = delays[p99_index]
        errors = sum(1 for _ in range(trials) if rng.random() < error_probability)
        err_rate = errors / trials
        return p99, err_rate

    def _format_test_output(self, p99: float, err_rate: float, passed: bool) -> str:
        verdict = "PASSED" if passed else "FAILED"
        return (
            f"Simulated checkout load test ({verdict}, {500} requests):\n"
            f"  p99 latency: {p99:.0f}ms (bound: <= {self.max_latency_ms:.0f}ms)\n"
            f"  error rate: {err_rate:.1%} (bound: <= {self.max_error_probability:.1%})"
        )
