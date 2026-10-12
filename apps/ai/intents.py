"""Intent layer output: intents, stages, entities, the pipeline result and reply IDs.

The intent layer never resolves entities. It returns raw, normalized customer text
(e.g. ``product_query="makhana"``); the conversation engine resolves it through the
Backend Port. No price, stock, product ID, order status or payment status is ever
produced here. ``reference_id`` only echoes an ID the engine itself put in a button
or list row.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum

LANGUAGES = ("en", "te", "hi")
UNITS = ("packet", "piece", "box", "bottle", "unit")


class Intent(StrEnum):
    GREETING = "GREETING"
    LANGUAGE_SELECT = "LANGUAGE_SELECT"
    PRODUCT_SEARCH = "PRODUCT_SEARCH"
    CATEGORY_SELECT = "CATEGORY_SELECT"
    PRODUCT_SELECT = "PRODUCT_SELECT"
    ADD_TO_CART = "ADD_TO_CART"
    REMOVE_FROM_CART = "REMOVE_FROM_CART"
    UPDATE_QUANTITY = "UPDATE_QUANTITY"
    VIEW_CART = "VIEW_CART"
    CHECKOUT = "CHECKOUT"
    ORDER_STATUS = "ORDER_STATUS"
    PAYMENT_STATUS = "PAYMENT_STATUS"
    TRACKING = "TRACKING"
    HELP = "HELP"
    FALLBACK = "FALLBACK"
    # Produced only by the pipeline (low confidence). Never accepted from the LLM or a reply ID.
    CLARIFY = "CLARIFY"
    # Produced only by the deterministic "IVA PLAN v1" header parser (apps.ai.plan_message).
    # Never accepted from the LLM or a reply ID: the plan's fields are health-adjacent and
    # must only come from that strict parser, never be guessed or replayed via a button.
    PLAN_RECEIVED = "PLAN_RECEIVED"


# Intents the LLM may return.
CLASSIFIABLE_INTENTS = tuple(i for i in Intent if i not in (Intent.CLARIFY, Intent.PLAN_RECEIVED))
# Extra intent NAMES the Groq classifier may return when the recommendation flow is on
# (IVA_LLM_RECOMMEND_ENABLED). They are not members of ``Intent``: a recommendation result
# is an ordinary read-only PRODUCT_SEARCH result that carries a ``RecommendRequest``.
RECOMMEND_INTENT_NAMES = ("RECOMMEND", "DIET_PLAN", "PRODUCT_QUESTION")
# Intents a button/list reply ID may carry.
REPLY_INTENTS = tuple(i for i in CLASSIFIABLE_INTENTS if i is not Intent.FALLBACK)
# The only intents that carry an order number.
ORDER_INTENTS = (Intent.ORDER_STATUS, Intent.PAYMENT_STATUS, Intent.TRACKING)


class Stage(StrEnum):
    """Which pipeline stage decided the result."""

    BUTTON = "button"
    PLAN = "plan"
    RULES = "rules"
    FUZZY = "fuzzy"
    LLM = "llm"
    NONE = "none"


class LLMOutcome(StrEnum):
    NOT_CALLED = "not_called"  # an earlier stage was confident
    NOT_CONFIGURED = "not_configured"
    OK = "ok"
    LOW_CONFIDENCE = "low_confidence"
    TIMEOUT = "timeout"
    ERROR = "error"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    # New classifier path (apps.ai.llm_fallback) only:
    SKIPPED = "skipped"  # guard said no (breaker, daily cap, per-customer limit)
    UNKNOWN = "unknown"  # the model answered "unknown"
    AMBIGUOUS = "ambiguous"  # the model said it could mean several products


@dataclass(frozen=True)
class Entities:
    product_query: str | None = None
    category_query: str | None = None
    quantity: int | None = None
    unit: str | None = None
    language_choice: str | None = None
    order_number: str | None = None
    choice_index: int | None = None
    reference_id: str | None = None
    # PLAN_RECEIVED only, filled by apps.ai.plan_message: never guessed, never logged.
    goal: str | None = None
    diet: str | None = None
    calories: int | None = None
    protein_g: int | None = None
    picks: tuple[str, ...] = ()


# Entity fields that text understanding (rules or LLM) may fill. reference_id comes
# only from reply IDs.
TEXT_ENTITY_FIELDS = (
    "product_query",
    "category_query",
    "quantity",
    "unit",
    "language_choice",
    "order_number",
    "choice_index",
)

_ORDER_FIELDS = frozenset({"order_number"})
RELEVANT_ENTITIES: dict[Intent, frozenset[str]] = {
    Intent.GREETING: frozenset(),
    Intent.LANGUAGE_SELECT: frozenset({"language_choice"}),
    Intent.PRODUCT_SEARCH: frozenset({"product_query", "category_query"}),
    Intent.CATEGORY_SELECT: frozenset({"category_query"}),
    Intent.PRODUCT_SELECT: frozenset({"product_query", "choice_index"}),
    Intent.ADD_TO_CART: frozenset({"product_query", "quantity", "unit"}),
    Intent.REMOVE_FROM_CART: frozenset({"product_query", "choice_index"}),
    Intent.UPDATE_QUANTITY: frozenset({"product_query", "quantity", "unit", "choice_index"}),
    Intent.VIEW_CART: frozenset(),
    Intent.CHECKOUT: frozenset(),
    Intent.ORDER_STATUS: _ORDER_FIELDS,
    Intent.PAYMENT_STATUS: _ORDER_FIELDS,
    Intent.TRACKING: _ORDER_FIELDS,
    Intent.HELP: frozenset(),
    Intent.PLAN_RECEIVED: frozenset({"goal", "diet", "calories", "protein_g", "picks"}),
}


def keep_relevant(intent: Intent, entities: Entities, suggested: Intent | None = None) -> Entities:
    """Drop entity fields that mean nothing for ``intent``.

    FALLBACK and CLARIFY keep everything, so the engine can still use, say, a bare
    quantity while it is waiting for one, except ``order_number``: it is kept only for
    order intents (for CLARIFY, when the suggested intent is one).
    """
    if intent in (Intent.FALLBACK, Intent.CLARIFY):
        if intent is Intent.CLARIFY and suggested in ORDER_INTENTS:
            return entities
        return Entities(**{**asdict(entities), "order_number": None})
    allowed = RELEVANT_ENTITIES[intent] | {"reference_id"}
    return Entities(**{k: (v if k in allowed else None) for k, v in asdict(entities).items()})


@dataclass(frozen=True)
class LLMItem:
    """One product the LLM found in a message. ``product_slug`` is already checked
    against the catalog allow-list; the engine still re-checks it through the port."""

    product_slug: str | None
    mention: str | None
    quantity: int | None


@dataclass(frozen=True)
class RecommendRequest:
    """A validated recommendation-flow request. Constraints only: no products, no text for
    the customer. ``kind`` is recommend | diet_plan | product_question | medical | clarify."""

    kind: str
    category_slug: str | None = None
    diet_type: str | None = None
    max_price: Decimal | None = None
    min_price: Decimal | None = None
    sort: str | None = None
    unsupported: str | None = None  # a fixed label key (see apps.ai.recommend.UNSUPPORTED)
    product_mention: str | None = None
    asked_fact: str | None = None
    route: str = ""  # which stage handled it: cue | classifier | cue_medical (for logs)
    # Health wording is CONTEXT only: it adds fixed safety sentences and never changes
    # which products are chosen or how they are sorted.
    health_context: str = "none"
    asks_advice: bool = False
    meal_time: str | None = None
    sort_from_goal: bool = False  # the sort came from the goal table, not from the model
    # Constraint NAMES read in code from the message (price, diet, protein_sort, ...): for
    # the log line and nothing else. Never values, never customer text.
    cues: tuple[str, ...] = ()
    # Which constraint fields were read in CODE, and which only the model supplied (names only,
    # for the log line).
    code_keys: tuple[str, ...] = ()
    model_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    confidence: float
    stage: Stage
    entities: Entities = field(default_factory=Entities)
    detected_language: str | None = None  # best guess; never overrides the saved language
    suggested_intent: Intent | None = None  # set only when intent is CLARIFY
    llm_outcome: LLMOutcome = LLMOutcome.NOT_CALLED
    # Set only for Stage.LLM results; never carries more than slugs, mentions, quantities.
    llm_items: tuple[LLMItem, ...] = ()
    request: RecommendRequest | None = None  # recommendation flow only

    def to_dict(self) -> dict:
        data = asdict(self)
        data["intent"] = self.intent.value
        data["stage"] = self.stage.value
        data["suggested_intent"] = self.suggested_intent.value if self.suggested_intent else None
        data["llm_outcome"] = self.llm_outcome.value
        return data


# --------------------------------------------------------------------------- reply IDs
#
# Grammar:  INTENT | INTENT:ref | INTENT:ref:quantity
#   LANGUAGE_SELECT:<en|te|hi>                -> language_choice
#   ORDER_STATUS|PAYMENT_STATUS|TRACKING:<no> -> order_number
#   ADD_TO_CART|UPDATE_QUANTITY:<ref>:<qty>   -> reference_id + quantity
#   any other INTENT:<ref>                    -> reference_id
# WhatsApp allows up to 200 characters for a list row ID; refs are capped at 64.

MAX_REPLY_ID_LENGTH = 200
_REF_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_QTY_RE = re.compile(r"^\d{1,6}$")
_ORDER_REF_INTENTS = ORDER_INTENTS
_QTY_INTENTS = (Intent.ADD_TO_CART, Intent.UPDATE_QUANTITY)


def build_reply_id(
    intent: Intent | str, ref: str | None = None, quantity: int | None = None
) -> str:
    """Build a button/list reply ID that ``parse_reply_id`` understands."""
    intent = Intent(intent)
    if intent not in REPLY_INTENTS:
        raise ValueError("Intent cannot be used in a reply ID.")
    parts = [intent.value]
    if ref is not None:
        parts.append(ref)
    if quantity is not None:
        if ref is None or intent not in _QTY_INTENTS:
            raise ValueError("Quantity needs a reference and a cart intent.")
        parts.append(str(quantity))
    reply_id = ":".join(parts)
    if parse_reply_id(reply_id) is None:
        raise ValueError("Invalid reply ID parts.")
    return reply_id


def parse_reply_id(reply_id: object) -> tuple[Intent, Entities] | None:
    """Return ``(intent, entities)`` for a valid reply ID, else ``None``."""
    if not isinstance(reply_id, str) or not 0 < len(reply_id) <= MAX_REPLY_ID_LENGTH:
        return None
    parts = reply_id.split(":")
    if len(parts) > 3:
        return None
    try:
        intent = Intent(parts[0])
    except ValueError:
        return None
    if intent not in REPLY_INTENTS:
        return None
    if len(parts) == 1:
        return intent, Entities()

    ref = parts[1]
    if not _REF_RE.match(ref):
        return None
    if len(parts) == 3:
        if intent not in _QTY_INTENTS or not _QTY_RE.match(parts[2]):
            return None
        return intent, Entities(reference_id=ref, quantity=int(parts[2]))
    if intent is Intent.LANGUAGE_SELECT:
        return (intent, Entities(language_choice=ref)) if ref in LANGUAGES else None
    if intent in _ORDER_REF_INTENTS:
        return intent, Entities(order_number=ref)
    return intent, Entities(reference_id=ref)
