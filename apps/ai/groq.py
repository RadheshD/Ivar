"""Groq adapter for ``IntentClassifier`` (OpenAI-compatible chat completions).

Uses the project's own HTTP helper (``apps.iva_common.http_transport``); no SDK. The API
key is held only in the request header: it is never logged and never put in an exception
(errors carry a fixed ``kind`` only; response bodies are not echoed).

Structured output: models documented to support strict ``json_schema`` (the ``openai/
gpt-oss-*`` family) get it; any other model gets plain JSON mode. Either way the reply is
re-validated by ``apps.ai.classifier.parse_classification``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from apps.iva_common.http_transport import HttpError, HttpTransport, StdlibTransport

from .classifier import (
    LLM_INTENTS,
    CatalogEntry,
    ClassifierHttpError,
    ClassifierRateLimited,
    ClassifierReply,
    ClassifierTimeout,
    build_system_prompt,
    build_user_message,
    response_schema,
)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"
MAX_COMPLETION_TOKENS = 600  # gpt-oss reasons first; the visible JSON is ~100 tokens
TEMPERATURE = 0.1
CONNECT_TIMEOUT = 3.0
_STRICT_SCHEMA_PREFIXES = ("openai/gpt-oss-",)


class GroqClassifier:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: HttpTransport | None = None,
        extra_intents: Sequence[str] = (),
    ):
        if not api_key:
            raise ValueError("A Groq API key is required.")
        self._api_key = api_key
        self.model = model or DEFAULT_MODEL
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._transport = transport if transport is not None else StdlibTransport()
        self.extra_intents = tuple(extra_intents)

    def __repr__(self) -> str:  # never show the key
        return f"GroqClassifier(model={self.model!r})"

    def _body(
        self,
        system: str,
        user: str,
        schema: dict,
        schema_name: str = "iva_intent",
    ) -> dict:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": TEMPERATURE,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
        }
        if self.model.startswith(_STRICT_SCHEMA_PREFIXES):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
            body["reasoning_effort"] = "low"
            body["include_reasoning"] = False
        else:
            body["response_format"] = {"type": "json_object"}
        return body

    def classify(
        self, message: str, *, catalog: Sequence[CatalogEntry], timeout: float
    ) -> ClassifierReply:
        body = self._body(
            build_system_prompt(catalog, self.extra_intents),
            build_user_message(message),
            response_schema([i.value for i in LLM_INTENTS] + list(self.extra_intents)),
        )
        return self._send(body, timeout)

    def extract(
        self, message: str, *, categories: Sequence[tuple[str, str]], timeout: float
    ) -> ClassifierReply:
        """The recommendation extraction call: constraints only, no product list."""
        from .recommend import build_extraction_prompt, extraction_schema

        body = self._body(
            build_extraction_prompt(categories),
            build_user_message(message),
            extraction_schema(),
            "iva_request",
        )
        return self._send(body, timeout)

    def _send(self, body: dict, timeout: float) -> ClassifierReply:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._transport.request(
                "POST",
                self._url,
                headers=headers,
                body=payload,
                connect_timeout=min(CONNECT_TIMEOUT, timeout),
                read_timeout=timeout,
            )
        except HttpError as exc:
            if exc.kind == "timeout":
                raise ClassifierTimeout() from None
            raise ClassifierHttpError("connection") from None
        if response.status == 429:
            raise ClassifierRateLimited()
        if response.status != 200:
            raise ClassifierHttpError("http_error")
        try:
            data = json.loads(response.body)
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise ClassifierHttpError("bad_response") from None
        if not isinstance(text, str):
            raise ClassifierHttpError("bad_response")
        return ClassifierReply(
            text=text,
            model=str(data.get("model") or self.model)[:80],
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
        )


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
