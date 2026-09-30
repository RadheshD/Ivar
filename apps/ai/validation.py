"""Strict validation of LLM output, standard library only.

Expected shape (every key required, no extra keys, JSON object only):

    {
      "intent": "<one of CLASSIFIABLE_INTENTS>",
      "confidence": <number 0..1>,
      "entities": {
        "product_query": <string|null>, "category_query": <string|null>,
        "quantity": <integer|null>, "unit": <one of UNITS|null>,
        "language_choice": <"en"|"te"|"hi"|null>, "order_number": <string|null>,
        "choice_index": <integer >= 1|null>
      }
    }

Error messages are fixed strings: they never echo the model's output.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from .intents import CLASSIFIABLE_INTENTS, LANGUAGES, TEXT_ENTITY_FIELDS, UNITS, Entities, Intent

MAX_OUTPUT_LENGTH = 4000
MAX_TEXT_FIELD_LENGTH = 100
_TOP_LEVEL_KEYS = frozenset({"intent", "confidence", "entities"})
_ENTITY_KEYS = frozenset(TEXT_ENTITY_FIELDS)
_TEXT_FIELDS = ("product_query", "category_query")
_ORDER_NUMBER_RE = re.compile(r"^[A-Za-z0-9\-]{1,32}$")


class LLMOutputError(ValueError):
    """LLM output rejected. ``kind`` is "invalid_json" or "invalid_schema"."""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True)
class ValidatedIntent:
    intent: Intent
    confidence: float
    entities: Entities


def _schema_error(reason: str) -> LLMOutputError:
    return LLMOutputError("invalid_schema", reason)


def _reject_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise LLMOutputError("invalid_json", "duplicate key")
    return dict(pairs)


def _reject_constant(_name):
    raise LLMOutputError("invalid_json", "non-finite number")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_text(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _schema_error(f"{field} must be a string or null")
    value = value.strip()
    if not value or len(value) > MAX_TEXT_FIELD_LENGTH:
        raise _schema_error(f"{field} has an invalid length")
    if any(not ch.isprintable() for ch in value):
        raise _schema_error(f"{field} contains control characters")
    return value


def _entities(data) -> Entities:
    if not isinstance(data, dict):
        raise _schema_error("entities must be an object")
    keys = set(data)
    if keys - _ENTITY_KEYS:
        raise _schema_error("unexpected entity field")
    if _ENTITY_KEYS - keys:
        raise _schema_error("missing entity field")

    values = {field: _optional_text(data[field], field) for field in _TEXT_FIELDS}

    quantity = data["quantity"]
    if quantity is not None and not _is_int(quantity):
        raise _schema_error("quantity must be an integer or null")
    choice = data["choice_index"]
    if choice is not None and (not _is_int(choice) or choice < 1):
        raise _schema_error("choice_index must be a positive integer or null")
    unit = data["unit"]
    if unit is not None and unit not in UNITS:
        raise _schema_error("unit is not allowed")
    language = data["language_choice"]
    if language is not None and language not in LANGUAGES:
        raise _schema_error("language_choice is not allowed")
    order_number = data["order_number"]
    if order_number is not None and (
        not isinstance(order_number, str) or not _ORDER_NUMBER_RE.match(order_number)
    ):
        raise _schema_error("order_number is invalid")

    return Entities(
        **values,
        quantity=quantity,
        unit=unit,
        language_choice=language,
        order_number=order_number.upper() if order_number else None,
        choice_index=choice,
    )


def parse_llm_output(raw: object) -> ValidatedIntent:
    """Parse and validate raw LLM output, or raise ``LLMOutputError``."""
    if not isinstance(raw, str) or not raw.strip():
        raise LLMOutputError("invalid_json", "empty or non-text output")
    if len(raw) > MAX_OUTPUT_LENGTH:
        raise LLMOutputError("invalid_json", "output too long")
    try:
        data = json.loads(
            raw, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except LLMOutputError:
        raise
    except ValueError:
        raise LLMOutputError("invalid_json", "not valid JSON") from None

    if not isinstance(data, dict):
        raise _schema_error("top level must be an object")
    keys = set(data)
    if keys - _TOP_LEVEL_KEYS:
        raise _schema_error("unexpected field")
    if _TOP_LEVEL_KEYS - keys:
        raise _schema_error("missing field")

    intent_value = data["intent"]
    if not isinstance(intent_value, str) or intent_value not in CLASSIFIABLE_INTENTS:
        raise _schema_error("unknown intent")

    confidence = data["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise _schema_error("confidence must be a number from 0 to 1")

    return ValidatedIntent(Intent(intent_value), float(confidence), _entities(data["entities"]))
