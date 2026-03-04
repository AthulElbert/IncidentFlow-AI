import json
import ssl
from typing import Protocol
from urllib import request
from urllib.error import HTTPError, URLError

from app.models.schemas import APMEvent


class LLMClient(Protocol):
    def triage(self, event: APMEvent) -> dict:
        ...


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

    def triage(self, event: APMEvent) -> dict:
        url = f"{self.base_url}/chat/completions"
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

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt)},
            ],
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
            raise RuntimeError(f"LLM API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM connection error: {exc.reason}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("LLM content is not an object")
            return parsed
        except Exception as exc:
            raise RuntimeError(f"Invalid LLM triage payload: {body}") from exc
