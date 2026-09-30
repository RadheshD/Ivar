"""Provider-agnostic LLM interface for the fallback stage.

No real provider is implemented yet and no SDK or HTTP library is used. A future
adapter implements ``LLMClient.complete`` and passes ``timeout`` to its own HTTP call.
``FakeLLMClient`` is for tests and local development only.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .intents import CLASSIFIABLE_INTENTS, LANGUAGES, TEXT_ENTITY_FIELDS, UNITS

DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_PROMPT_PRODUCT_NAMES = 60
MAX_PROMPT_CATEGORY_NAMES = 30

# Provider names that client_from_settings() can build. Empty until a provider is chosen.
PROVIDERS: dict[str, type] = {}


class LLMError(Exception):
    """The LLM call failed."""


class LLMTimeout(LLMError):
    """The LLM call did not finish in time."""


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, prompt: str, *, timeout: float) -> str:
        """Return the model's raw text reply. Raise ``LLMTimeout``/``LLMError`` on failure."""
        ...


def build_prompt(
    message: str,
    *,
    language_hint: str | None = None,
    product_names: Iterable[str] = (),
    category_names: Iterable[str] = (),
) -> str:
    """Build the classification prompt. ``message`` must already be masked."""
    schema = {
        "intent": "one of the allowed intents",
        "confidence": "number from 0 to 1",
        "entities": {name: None for name in TEXT_ENTITY_FIELDS},
    }
    lines = [
        "You classify one WhatsApp message sent to IVA, the food-ordering assistant of Ivar.",
        "Reply with exactly one JSON object and nothing else, with exactly these keys:",
        json.dumps(schema),
        "Allowed intents: " + ", ".join(i.value for i in CLASSIFIABLE_INTENTS) + ".",
        "Allowed units: " + ", ".join(UNITS) + ". Allowed language_choice: "
        + ", ".join(LANGUAGES) + ".",
        "product_query and category_query are the customer's own words, in English if you "
        "can translate them. Never invent prices, stock, product IDs, order status or "
        "payment status. Use null for anything the customer did not say. If unsure, give "
        "a low confidence.",
        "Known product names: "
        + json.dumps(list(product_names)[:MAX_PROMPT_PRODUCT_NAMES], ensure_ascii=False),
        "Known category names: "
        + json.dumps(list(category_names)[:MAX_PROMPT_CATEGORY_NAMES], ensure_ascii=False),
        f"Message language guess: {language_hint or 'unknown'}",
        "Customer message (JSON string): " + json.dumps(message, ensure_ascii=False),
    ]  # fmt: skip
    return "\n".join(lines)


class FakeLLMClient:
    """Scripted LLM client for tests and local development. Never used in production.

    ``responses`` are returned in order, then ``default``. An exception instance is
    raised instead of returned. ``delay`` sleeps before answering, to exercise timeouts.
    """

    def __init__(
        self,
        responses: Iterable[str | BaseException] = (),
        *,
        default: str | BaseException | None = None,
        delay: float = 0.0,
    ):
        self._responses = deque(responses)
        self._default = default
        self.delay = delay
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def complete(self, prompt: str, *, timeout: float) -> str:
        self.prompts.append(prompt)
        if self.delay:
            time.sleep(self.delay)
        item = self._responses.popleft() if self._responses else self._default
        if item is None:
            raise LLMError("FakeLLMClient has no scripted response.")
        if isinstance(item, BaseException):
            raise item
        return item

    @staticmethod
    def reply(intent: str, confidence: float = 0.9, **entities) -> str:
        """A well-formed JSON reply; unspecified entities are null."""
        unknown = set(entities) - set(TEXT_ENTITY_FIELDS)
        if unknown:
            raise ValueError("Unknown entity field.")
        body = {name: entities.get(name) for name in TEXT_ENTITY_FIELDS}
        return json.dumps({"intent": intent, "confidence": confidence, "entities": body})


def client_from_settings() -> LLMClient | None:
    """Build the configured client, or ``None`` when ``LLM_PROVIDER`` is empty."""
    provider = (getattr(settings, "LLM_PROVIDER", "") or "").strip().lower()
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise ImproperlyConfigured("LLM_PROVIDER names a provider that is not implemented.")
    return PROVIDERS[provider](
        api_key=getattr(settings, "LLM_API_KEY", ""),
        model=getattr(settings, "LLM_MODEL", ""),
    )


def timeout_from_settings() -> float:
    try:
        value = float(getattr(settings, "LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        raise ImproperlyConfigured("LLM_TIMEOUT_SECONDS must be a number.") from None
    if value <= 0:
        raise ImproperlyConfigured("LLM_TIMEOUT_SECONDS must be greater than 0.")
    return value
