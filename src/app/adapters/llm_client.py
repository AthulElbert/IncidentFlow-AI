import json
import re
import ssl
from dataclasses import dataclass
from typing import Protocol
from urllib import request
from urllib.error import HTTPError, URLError

from app.models.schemas import APMEvent

# HTTP statuses worth retrying: the request may succeed if sent again.
TRANSIENT_STATUSES = frozenset({408, 425, 429})
# A reply wrapped in a markdown code fence: ```json ... ```
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
# Google's APIs say how long to wait in the error body (RetryInfo), e.g. "retryDelay": "41s".
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


class LLMError(RuntimeError):
    """A model call failed. `transient` means the same request may succeed if retried.

    `retry_after` is how many seconds the provider asked us to wait before trying again, when it said.
    """

    def __init__(
        self, message: str, status: int | None = None, transient: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.retry_after = retry_after


def parse_json_object(content: str) -> dict:
    """The JSON object in a model's reply, tolerating what models commonly wrap around it.

    Seen in practice: a code fence around the object, or a separator such as `---` before it. Only the first
    complete object is taken. Being lenient here is safe because every caller validates the object's fields
    (allowed actions, tools and values) before acting on it.
    """
    text = content.strip()
    try:
        value = json.loads(text)
    except ValueError:
        unfenced = _FENCE.sub("", text).strip()
        start = unfenced.find("{")
        if start < 0:
            raise ValueError("no JSON object in the reply") from None
        value, _ = json.JSONDecoder().raw_decode(unfenced[start:])
    if not isinstance(value, dict):
        raise ValueError("LLM content is not an object")
    return value


def retry_after_seconds(header: str | None, body: str) -> float | None:
    """How long the provider asked us to wait: the Retry-After header (seconds), or a retryDelay in the body.

    A Retry-After given as an HTTP date is ignored (rare for model APIs); the caller then uses its own backoff.
    """
    if header:
        try:
            value = float(header.strip())
            if value >= 0:
                return value
        except ValueError:
            pass
    match = _RETRY_DELAY.search(body or "")
    return float(match.group(1)) if match else None


@dataclass(frozen=True)
class LLMReply:
    content: dict
    prompt_tokens: int | None = None  # None when the provider did not report usage
    completion_tokens: int | None = None


class LLMClient(Protocol):
    def triage(self, event: APMEvent) -> dict:
        ...

    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        ...

    def complete_json(self, messages: list[dict], error_label: str = "LLM") -> dict:
        ...


def triage_messages(event: APMEvent) -> list[dict]:
    system_prompt = (
        "You are an SRE triage assistant. Return ONLY valid JSON with keys: "
        "issue_type, confidence, probable_cause, hypothesis_steps. "
        "issue_type must be one of: performance_degradation, application_error, "
        "resource_saturation, dependency_failure, unknown. "
        "confidence must be 0..1. hypothesis_steps must be a short array of strings."
    )
    user_prompt = {
        "service": event.service,
        "metric": event.metric,
        "value": event.value,
        "threshold": event.threshold,
        "environment": event.environment,
        "message": event.message,
        "timestamp": event.timestamp.isoformat(),
    }
    return _system_and_user(system_prompt, user_prompt)


def code_fix_messages(file_path: str, file_content: str, instructions: str) -> list[dict]:
    system_prompt = (
        "You are an SRE automation agent that fixes a single misconfigured file. "
        "Return ONLY valid JSON with keys: fixed_content, summary. "
        "fixed_content must be a string containing the COMPLETE corrected file content "
        "(same format as the input file, e.g. valid JSON if the input is JSON). "
        "summary must be one short sentence explaining what changed and why. "
        "Do not include any commentary outside the JSON object, and do not wrap "
        "fixed_content in markdown code fences."
    )
    user_prompt = {
        "file_path": file_path,
        "current_content": file_content,
        "instructions": instructions,
    }
    return _system_and_user(system_prompt, user_prompt)


def _system_and_user(system_prompt: str, user_payload: dict) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload)},
    ]


MAX_ERROR_DETAIL = 600


def _describe_reply(body: object) -> str:
    """A short description of an unusable reply: why it ended and the start of what it said, not the whole body."""
    try:
        choice = body["choices"][0]  # type: ignore[index]
        content = str((choice.get("message") or {}).get("content", ""))
        return f"finish_reason={choice.get('finish_reason')!r}, content starts {content[:120]!r}"
    except Exception:
        return f"unexpected response shape: {str(body)[:200]}"


def _token_count(usage: object, key: str) -> int | None:
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) and value >= 0 else None


class OpenAICompatibleLLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 20,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl

        if not all([self.base_url, self.api_key, self.model]):
            raise ValueError("LLM config is incomplete for llm mode")

    def complete(self, messages: list[dict], error_label: str = "LLM") -> LLMReply:
        """One chat turn; the reply must be a JSON object. Also returns the token usage the provider reported."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        context = None
        if not self.verify_ssl:
            context = ssl._create_unverified_context()

        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=context) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            transient = exc.code in TRANSIENT_STATUSES or exc.code >= 500
            wait = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None, detail)
            raise LLMError(
                f"LLM API error {exc.code}: {detail[:MAX_ERROR_DETAIL]}", status=exc.code, transient=transient, retry_after=wait
            ) from exc
        except URLError as exc:
            raise LLMError(f"LLM connection error: {exc.reason}", transient=True) from exc
        except TimeoutError as exc:  # a read that stalls after the connection was made
            raise LLMError(f"LLM connection error: timed out after {self.timeout_seconds}s", transient=True) from exc
        except (ValueError, OSError) as exc:  # a body that is not JSON, or a dropped connection
            raise LLMError(f"LLM connection error: {exc}", transient=True) from exc

        try:
            content = body["choices"][0]["message"]["content"]
            parsed = parse_json_object(content)
        except Exception as exc:
            # A model sometimes returns a malformed sample (seen with Gemini: finish_reason MALFORMED_FUNCTION_CALL and
            # a junk character as content). Asking again usually works, so this is retryable; the router bounds it.
            raise LLMError(f"Invalid {error_label} payload: {_describe_reply(body)}", transient=True) from exc

        usage = body.get("usage") if isinstance(body, dict) else None
        return LLMReply(parsed, _token_count(usage, "prompt_tokens"), _token_count(usage, "completion_tokens"))

    def complete_json(self, messages: list[dict], error_label: str = "LLM") -> dict:
        """One chat turn over an arbitrary message list; the reply must be a JSON object."""
        return self.complete(messages, error_label).content

    def triage(self, event: APMEvent) -> dict:
        return self.complete_json(triage_messages(event), error_label="LLM triage")

    def propose_code_fix(self, file_path: str, file_content: str, instructions: str) -> dict:
        return self.complete_json(code_fix_messages(file_path, file_content, instructions), error_label="LLM code-fix")
