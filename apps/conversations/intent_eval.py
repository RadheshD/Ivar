"""Evaluation library behind ``manage.py eval_intent_llm`` (development only).

Scores the Groq classifier / recommendation extraction against the golden sets AND shows
what the real engine would have done with each answer. The "final engine action" is not
re-implemented here: the model's raw reply goes through the real validation
(``parse_classification`` / ``parse_extraction``), the real pipeline mapping
(``_from_classification`` / ``_from_extraction``) and the real engine handler
(``ConversationEngine._intent``) against the Fake Backend standing in for the Backend Port.

Nothing here touches the network; the command decides where raw replies come from (live
Groq, or canned replies for ``--dry-run``). Everything printed or saved is golden-set text:
never customer data, never the API key.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from apps.ai import classifier as cls
from apps.ai import constraints
from apps.ai.intents import Intent, IntentResult, LLMOutcome, Stage
from apps.ai.llm_fallback import ExtractionResult, FallbackResult, LLMFallback
from apps.ai.llm_guard import LLMGuard
from apps.ai.pipeline import IntentPipeline
from apps.ai.recommend import (
    any_explicit_sort,
    emergency_matches,
    medical_matches,
    parse_extraction,
    routing_cue_matches,
)
from apps.ai.text import tokenize

from .backend_port import MAX_LINE_QUANTITY, MAX_PAGE_SIZE, NotFound
from .channel import Inbound, OutButtons, OutCarousel, OutList, OutProductCard
from .engine import ConversationEngine, _Turn
from .fake_backend import FakeBackend
from .state import InMemorySessionStore, Session

GOLDEN = Path(__file__).resolve().parents[1] / "ai" / "golden" / "intent_golden.json"
RECOMMEND_GOLDEN = GOLDEN.with_name("recommend_golden.json")
RECOMMEND_GOALS_GOLDEN = GOLDEN.with_name("recommend_goals_golden.json")
RECOMMEND_HEALTH_GOLDEN = GOLDEN.with_name("recommend_health_golden.json")
RECOMMEND_PRICE_GOLDEN = GOLDEN.with_name("recommend_price_golden.json")
RECOMMEND_RANGE_GOLDEN = GOLDEN.with_name("recommend_range_golden.json")
RECOMMEND_ADVICE_GOLDEN = GOLDEN.with_name("recommend_advice_golden.json")
RECOMMEND_DETERMINISM_GOLDEN = GOLDEN.with_name("recommend_determinism_golden.json")
SET_NAMES = (
    "intents", "recommend", "recommend_goals", "recommend_health", "recommend_price",
    "recommend_range", "recommend_advice", "recommend_determinism",
)  # fmt: skip
PRICE_SETS = (
    "recommend_price",
    "recommend_range",
    "recommend_determinism",
)  # also checked against their price bounds

# Request types that hand a customer to products or advice; wrongly entering one of
# these is a "wrong hand-off".
HANDOFF_TYPES = ("recommend", "diet_plan", "product_question", "medical")
CART_INTENTS = frozenset({"ADD_TO_CART", "REMOVE_FROM_CART", "UPDATE_QUANTITY"})

NO_ACTION = "NO_ACTION"
READ_ONLY_REPLY = "READ_ONLY_REPLY"
CLARIFY = "CLARIFY"
CART_CONFIRMATION_PROMPT = "CART_CONFIRMATION_PROMPT"
PLANNER_HANDOFF = "PLANNER_HANDOFF"
SAFETY_REPLY = "SAFETY_REPLY"  # a fixed safety sentence (emergency, medical, allergy)
ACTION_CLASSES = (
    NO_ACTION,
    READ_ONLY_REPLY,
    CLARIFY,
    CART_CONFIRMATION_PROMPT,
    PLANNER_HANDOFF,
    SAFETY_REPLY,
)

_CONSTRAINT_FIELDS = (
    "category_slug",
    "diet_type",
    "max_price",
    "min_price",
    "sort",
    "unsupported_constraint",
)


# --------------------------------------------------------------------------- golden sets


def load_golden(path: Path = GOLDEN) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


def load_recommend_golden(path: Path = RECOMMEND_GOLDEN) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


def is_recommend(set_name: str) -> bool:
    """Both recommendation golden sets are scored as recommendation sets."""
    return set_name.startswith("recommend")


def load_entries(set_name: str) -> list[dict]:
    if set_name == "recommend_goals":
        return load_recommend_golden(RECOMMEND_GOALS_GOLDEN)
    if set_name == "recommend_health":
        return load_recommend_golden(RECOMMEND_HEALTH_GOLDEN)
    if set_name == "recommend_price":
        return load_recommend_golden(RECOMMEND_PRICE_GOLDEN)
    if set_name == "recommend_range":
        return load_recommend_golden(RECOMMEND_RANGE_GOLDEN)
    if set_name == "recommend_advice":
        return load_recommend_golden(RECOMMEND_ADVICE_GOLDEN)
    if set_name == "recommend_determinism":
        return load_recommend_golden(RECOMMEND_DETERMINISM_GOLDEN)
    return load_recommend_golden() if set_name == "recommend" else load_golden()


def catalog_from_backend(backend) -> tuple[cls.CatalogEntry, ...]:
    names = {
        c.id: c.name for g in backend.list_categories(None) for c in backend.list_categories(g.id)
    }
    entries, offset = [], 0
    while True:
        page, total = backend.list_products(limit=MAX_PAGE_SIZE, offset=offset)
        entries += [cls.CatalogEntry(p.id, p.name, names.get(p.category_id, "")) for p in page]
        offset += len(page)
        if not page or offset >= total:
            return tuple(entries)


def categories_from_backend(backend) -> tuple[tuple[str, str], ...]:
    return tuple(
        (c.id, c.name)
        for g in backend.list_categories(None)
        for c in (g, *backend.list_categories(g.id))
    )


# --------------------------------------------------------------------------- raw scoring


def _num(value):
    return None if value is None else float(value)


def score_recommend(entry: dict, extraction, min_confidence: float) -> dict:
    """Scores for one recommendation golden entry. A failed call counts as 'other'.

    ``type_ok``: request type matches (or is in the entry's ``accept`` list).
    ``constraints_ok``: for recommend/diet_plan entries, every constraint matches; for the
    other types only the type matters (it returns True).
    ``wrong_handoff``: the model sent the customer into a hand-off type the entry does not
    expect. ``unsafe``: a medical/allergy entry that came back as recommend or
    product_question (it would have shown product picks for a health question).
    """
    usable = extraction is not None and extraction.confidence >= min_confidence
    predicted = extraction.request_type if usable and not extraction.ambiguous else None
    # An ambiguous or low-confidence answer is shown as a clarifying question, which is a
    # safe non-hand-off; it is right only for entries that expect ambiguity.
    expected = entry["request_type"]
    accepted = {expected, *entry.get("accept", [])}
    if predicted is None:
        type_ok = bool(entry.get("ambiguous")) or "other" in accepted and expected == "other"
        constraints_ok = bool(entry.get("ambiguous"))
        return {"type_ok": type_ok, "constraints_ok": constraints_ok, "wrong_handoff": False,
                "unsafe": False, "predicted": "clarify/none"}  # fmt: skip
    type_ok = predicted in accepted
    constraints_ok = type_ok
    if type_ok and expected in ("recommend", "diet_plan") and not entry.get("accept"):
        r = extraction.request
        got = (
            r.category_slug,
            r.diet_type,
            _num(r.max_price),
            _num(r.min_price),
            r.sort,
            r.unsupported,
        )
        want = (
            entry["category_slug"], entry["diet_type"], _num(entry["max_price"]),
            _num(entry["min_price"]), entry["sort"], entry["unsupported"],
        )  # fmt: skip
        if "meal_time" in entry and r.meal_time != entry["meal_time"]:
            return {"type_ok": type_ok, "constraints_ok": False, "wrong_handoff": False,
                    "unsafe": False, "predicted": predicted}  # fmt: skip
        if "sort_accept" in entry:  # the goal table may add the sort in code, so null is fine
            got, want = got[:4] + got[5:], want[:4] + want[5:]
            constraints_ok = got == want and r.sort in entry["sort_accept"]
        else:
            constraints_ok = got == want
    wrong = predicted in HANDOFF_TYPES and predicted not in accepted
    unsafe = expected == "medical" and predicted in ("recommend", "product_question")
    return {"type_ok": type_ok, "constraints_ok": constraints_ok, "wrong_handoff": wrong,
            "unsafe": unsafe, "predicted": predicted}  # fmt: skip


def score(
    entry: dict, result: cls.Classification | None, min_confidence: float
) -> tuple[bool, bool]:
    """(intent_ok, items_ok) for one golden entry. A failed call counts as 'unknown'."""
    usable = result is not None and result.confidence >= min_confidence and not result.ambiguous
    predicted = result.intent if usable else "unknown"
    intent_ok = predicted == entry["intent"]
    expected = {(i["slug"], i["quantity"]) for i in entry["items"]}
    got = (
        {(i.product_slug, i.quantity) for i in result.items if i.product_slug} if usable else set()
    )
    items_ok = got == expected
    return intent_ok, items_ok


# --------------------------------------------------------------------------- canned replies


def canned_reply(entry: dict, set_name: str) -> str:
    """The ideal model reply for a golden entry, used by ``--dry-run`` (no network)."""
    if is_recommend(set_name):
        return json.dumps(
            {
                "request_type": entry["request_type"],
                "category_slug": entry["category_slug"],
                "diet_type": entry["diet_type"],
                "max_price": entry["max_price"],
                "min_price": entry["min_price"],
                "sort": entry["sort"],
                "product_mention": entry["product_mention"],
                "asked_fact": entry["asked_fact"],
                "unsupported_constraint": entry["unsupported"],
                "health_context": entry.get("health_context", "none"),
                "asks_advice": bool(entry.get("asks_advice", False)),
                "meal_time": entry.get("meal_time"),
                "language": {"hi": "hi", "te": "te", "en": "en"}.get(entry["language"], "other"),
                "confidence": 0.9 if entry.get("ambiguous") else 0.95,
                "ambiguous": bool(entry.get("ambiguous")),
            }
        )
    return json.dumps(
        {
            "intent": entry["intent"],
            "confidence": 0.95,
            "items": [
                {"product_slug": i["slug"], "mention": i["slug"], "quantity": i["quantity"]}
                for i in entry["items"]
            ],
            "language": {"hi": "hi", "te": "te", "en": "en"}.get(entry["language"], "other"),
            "ambiguous": False,
        }
    )


# --------------------------------------------------------------------------- harness


class _NoNetwork:
    """Classifier stub for the harness' own LLMFallback: it must never be called."""

    def classify(self, *args, **kwargs):
        raise AssertionError("the eval harness must not call the classifier")

    extract = classify


def _loose_json(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def classify_action(replies) -> str:
    """Action class from the reply template keys of the real engine output."""
    keys = [m.key for m in replies if getattr(m, "key", None)]
    if any(k.startswith("llm.confirm") for k in keys):
        return CART_CONFIRMATION_PROMPT
    if any(k.startswith("safety.") for k in keys):
        return SAFETY_REPLY
    if any(k in ("recommend.diet_plan", "planner.cta_body") for k in keys):
        return PLANNER_HANDOFF
    if any(k == "recommend.clarify" or k.startswith("clarify.") for k in keys):
        return CLARIFY
    if not keys or set(keys) <= {"fallback"}:
        return NO_ACTION
    return READ_ONLY_REPLY


_REF_RE = re.compile(r"^(?:ACT:(?:addcard|ordernow|showcard):|PRODUCT_SELECT:)([^:]+)$")


def _reply_ids(message) -> Iterable[str]:
    if isinstance(message, OutButtons | OutProductCard):
        yield from (b.id for b in message.buttons)
    elif isinstance(message, OutCarousel):
        for card in message.cards:
            yield from (b.id for b in card.buttons)
    elif isinstance(message, OutList):
        yield from (r.id for r in message.rows)


def shown_slugs(replies, session: Session) -> list[str]:
    """Product IDs the replies show or offer (from card/list reply IDs)."""
    found: list[str] = []
    for message in replies:
        for reply_id in _reply_ids(message):
            match = _REF_RE.match(reply_id)
            if match:
                slug = session.refs.get(match.group(1), match.group(1))
                if slug not in found:
                    found.append(slug)
    return found


def _kept_dropped(raw_items, validated_items):
    kept = [i.product_slug for i in validated_items if i.product_slug]
    raw_slugs = [i.get("product_slug") for i in raw_items if isinstance(i, dict)]
    dropped = [s for s in raw_slugs if isinstance(s, str) and s not in kept]
    nulled = [
        (r.get("product_slug") or r.get("mention"))
        for r, v in zip(raw_items, validated_items, strict=False)
        if isinstance(r, dict) and r.get("quantity") is not None and v.quantity is None
    ]
    return kept, dropped, nulled


class EvalIsolationError(RuntimeError):
    """The eval's backend could be writing somewhere it must not (the dev state file)."""


def isolated_backend() -> FakeBackend:
    """A Fake Backend that lives only in memory for this eval run.

    ``state_file=""`` is explicit on purpose: the default (``None``) would read
    ``IVA_FAKE_BACKEND_STATE_FILE`` from settings and the eval's throwaway customers and
    carts would be saved into the developer's real dev state. Product images are also
    pinned off so results do not depend on the developer's ``.env``.
    """
    backend = FakeBackend(state_file="", image_base_url="")
    assert_isolated(backend)
    return backend


def assert_isolated(backend) -> None:
    """Raise unless ``backend`` is a Fake Backend with no state file (fail loudly)."""
    if not isinstance(backend, FakeBackend):
        raise EvalIsolationError("The eval needs a Fake Backend it can isolate.")
    if getattr(backend, "_state_file", "missing") is not None:
        raise EvalIsolationError("Refusing to run: the backend would persist state to a file.")


@contextlib.contextmanager
def quiet_logs(enabled: bool = True):
    """Silence INFO (the per-call backend log lines) for the duration; warnings still show."""
    previous = logging.root.manager.disable
    if enabled:
        logging.disable(logging.INFO)
    try:
        yield
    finally:
        logging.disable(previous)


class Harness:
    """Runs raw model replies through the real decision code against the Fake Backend."""

    def __init__(self, min_confidence: float = 0.7, backend=None):
        self.backend = backend if backend is not None else isolated_backend()
        assert_isolated(self.backend)  # whichever way it was built: fail loudly if not isolated
        self.catalog = catalog_from_backend(self.backend)
        self.slugs = frozenset(e.slug for e in self.catalog)
        self.categories = categories_from_backend(self.backend)
        self.category_slugs = frozenset(slug for slug, _ in self.categories)
        self.min_confidence = min_confidence
        self.fallback = LLMFallback(
            _NoNetwork(), LLMGuard(), min_confidence=min_confidence, recommend=True
        )
        self.pipeline = IntentPipeline(fallback=self.fallback)
        self.engine = ConversationEngine(self.backend, InMemorySessionStore(), self.pipeline)
        self._customers = 0

    @staticmethod
    def _tokens(text: str) -> list[str]:
        """Tokens as the live pipeline builds them (a price range keeps its joiner word)."""
        if constraints.has_hyphen_range(text):
            ranged = tokenize(constraints.prepare_ranges(text))
            if constraints.parse(ranged).has_range:
                return ranged
        return tokenize(constraints.prepare(text))

    # -- one entry -----------------------------------------------------------------

    def evaluate(
        self, entry: dict, set_name: str, reply_text: str | None, error: str | None
    ) -> dict:
        record = {
            "id": entry["id"],
            "language": entry["language"],
            "set": set_name,
            "text": entry["text"],
            "needs_native_review": bool(entry.get("needs_native_review")),
            "api_error": error if reply_text is None else None,
        }
        if is_recommend(set_name):
            self._evaluate_recommend(entry, reply_text, error, record)
        else:
            self._evaluate_intent(entry, reply_text, error, record)
        return record

    def _error_outcome(self, error):
        return LLMOutcome.TIMEOUT if error == "timeout" else LLMOutcome.ERROR

    def _evaluate_intent(self, entry, reply_text, error, record):
        raw = _loose_json(reply_text)
        classification, outcome = None, self._error_outcome(error)
        if reply_text is not None:
            try:
                classification = cls.parse_classification(
                    reply_text, allowed_slugs=self.slugs, max_quantity=MAX_LINE_QUANTITY
                )
                outcome = self.fallback._judge(classification)
            except cls.ClassifierInvalid as exc:
                outcome = LLMOutcome(exc.kind)
        raw_items = (raw or {}).get("items") if isinstance((raw or {}).get("items"), list) else []
        record["expected"] = {"intent": entry["intent"], "items": entry["items"]}
        record["raw"] = (
            {
                "intent": raw.get("intent"),
                "confidence": raw.get("confidence"),
                "ambiguous": raw.get("ambiguous"),
                "language": raw.get("language"),
                "items": raw_items,
            }
            if raw
            else None
        )
        if classification:
            kept, dropped, nulled = _kept_dropped(raw_items, classification.items)
            record["validated"] = {
                "intent": classification.intent,
                "confidence": classification.confidence,
                "ambiguous": classification.ambiguous,
                "kept_slugs": kept,
                "dropped_slugs": dropped,
                "quantity_nulled": nulled,
                "items": [
                    {"slug": i.product_slug, "quantity": i.quantity} for i in classification.items
                ],
            }
        else:
            record["validated"] = None
        record["outcome"] = outcome.value
        result = self.pipeline._from_classification(FallbackResult(outcome, classification), None)
        if result is None:  # what the pipeline tail yields when the rules have no candidate
            result = IntentResult(Intent.FALLBACK, 0.0, Stage.NONE)
        intent_ok, items_ok = score(entry, classification, self.min_confidence)
        record["scores"] = {"intent_ok": intent_ok, "items_ok": items_ok}
        allowed = entry["intent"] in CART_INTENTS
        self._run_engine(entry, result, record, allowed_cart_change=allowed)
        record["failed"] = bool(
            not (intent_ok and items_ok) or record["api_error"] or record["safety"]
        )

    def _evaluate_recommend(self, entry, reply_text, error, record):
        raw = _loose_json(reply_text)
        extraction, outcome = None, self._error_outcome(error)
        if reply_text is not None:
            try:
                extraction = parse_extraction(reply_text, category_slugs=self.category_slugs)
                outcome = self.fallback.judge_extraction(extraction)
            except cls.ClassifierInvalid as exc:
                outcome = LLMOutcome(exc.kind)
        record["expected"] = {
            "request_type": entry["request_type"],
            "accept": entry.get("accept", []),
            "ambiguous": bool(entry.get("ambiguous")),
            **{k: entry[k] for k in ("health_context", "asks_advice", "meal_time") if k in entry},
            "action_class": entry.get("action_class"),
            "reply_keys": entry.get("reply_keys"),
            "constraints": {
                "category_slug": entry["category_slug"],
                "diet_type": entry["diet_type"],
                "max_price": entry["max_price"],
                "min_price": entry["min_price"],
                "sort": entry["sort"],
                "unsupported_constraint": entry["unsupported"],
            },
        }
        record["raw"] = (
            {
                "request_type": raw.get("request_type"),
                "confidence": raw.get("confidence"),
                "ambiguous": raw.get("ambiguous"),
                "language": raw.get("language"),
                "product_mention": raw.get("product_mention"),
                "asked_fact": raw.get("asked_fact"),
                "constraints": {k: raw.get(k) for k in _CONSTRAINT_FIELDS},
            }
            if raw
            else None
        )
        if extraction:
            r = extraction.request
            kept = {
                "category_slug": r.category_slug,
                "diet_type": r.diet_type,
                "max_price": None if r.max_price is None else float(r.max_price),
                "min_price": None if r.min_price is None else float(r.min_price),
                "sort": r.sort,
                "unsupported_constraint": r.unsupported,
            }
            dropped = [
                k for k in _CONSTRAINT_FIELDS if (raw or {}).get(k) is not None and kept[k] is None
            ]
            record["validated"] = {
                "request_type": extraction.request_type,
                "confidence": extraction.confidence,
                "ambiguous": extraction.ambiguous,
                "constraints": kept,
                "dropped_constraints": dropped,
            }
        else:
            record["validated"] = None
        record["outcome"] = outcome.value
        emergency_text = None
        if emergency_matches(entry["text"]):
            # Emergency keywords: code only, before rules and before any model call.
            result = IntentResult(Intent.FALLBACK, 0.0, Stage.NONE)
            record["route"] = "safety"
            emergency_text = entry["text"]
        elif entry.get("rules_priority"):
            # A cue plus a real product name: the live pipeline keeps the rules' answer.
            result = self.pipeline.understand(
                entry["text"],
                product_names=[e.name for e in self.catalog],
                category_names=[name for _slug, name in self.categories],
            )
            record["route"] = "rules"
        elif record["set"] in (*PRICE_SETS, "recommend_advice") and (
            cons := constraints.parse(self._tokens(entry["text"]))
        ):
            # Stated constraints: the live pipeline's constraint route (the extraction, with
            # the price / diet / ordering read in code applied over it).
            result = IntentPipeline.constraint_answer(
                ExtractionResult(outcome, extraction), None, entry["text"],
                self._tokens(entry["text"]), self.categories, cons,
            )  # fmt: skip
            record["route"] = "constraint"
        elif medical_matches(entry["text"]) and not any_explicit_sort(entry["text"]):
            # Health / weight-loss data: the live pipeline answers without asking the model.
            result = IntentPipeline._medical_result(None)
            record["route"] = "cue_medical"
        else:
            result = IntentPipeline._from_extraction(
                ExtractionResult(outcome, extraction), None, text=entry["text"], route="cue"
            )
            record["route"] = "cue"
        if result is None:  # request_type "other": the live pipeline hands over to the classifier
            result = IntentResult(Intent.FALLBACK, 0.0, Stage.NONE)
        scores = score_recommend(entry, extraction, self.min_confidence)
        record["scores"] = {
            "type_ok": scores["type_ok"],
            "constraints_ok": scores["constraints_ok"],
            "wrong_handoff": scores["wrong_handoff"],
            "unsafe": scores["unsafe"],
            "predicted": scores["predicted"],
        }
        self._run_engine(
            entry, result, record, allowed_cart_change=False, emergency_text=emergency_text
        )
        extra_ok = True
        if record["set"] in PRICE_SETS:
            high, low = entry["max_price"], entry["min_price"]
            out = [
                slug for slug in record["shown_slugs"]
                if (high is not None and self.backend.get_product(slug).price > Decimal(str(high)))
                or (low is not None and self.backend.get_product(slug).price < Decimal(str(low)))
            ]  # fmt: skip
            record["scores"]["price_ok"] = not out  # no shown item is outside the bounds
            extra_ok = not out
        if record["set"] == "recommend_determinism" and entry["sort"] == "protein_desc":
            grams = [self.backend.get_product(s).protein_g or 0 for s in record["shown_slugs"]]
            record["scores"]["sort_ok"] = grams == sorted(grams, reverse=True)
            extra_ok = extra_ok and record["scores"]["sort_ok"]
        if record["set"] == "recommend_advice":  # the code routes it to the flow, or not
            route_ok = routing_cue_matches(entry["text"]) or record["route"] == "constraint"
            record["scores"]["route_ok"] = route_ok == entry["cue"] or (
                not entry["cue"] and record["route"] == "constraint"
            )
            extra_ok = extra_ok and record["scores"]["route_ok"]
        if "action_class" in entry:  # the health golden set also pins the reply
            req = extraction.request if extraction else None
            skip_model = record["route"] in ("safety", "cue_medical", "rules")
            health_ok = skip_model or (
                req is not None and req.health_context == entry.get("health_context", "none")
            )
            advice_ok = skip_model or (
                req is not None and req.asks_advice == entry.get("asks_advice", False)
            )
            action_ok = record["action_class"] == entry["action_class"]
            expected_keys = entry["reply_keys"]
            keys_ok = record["reply_keys"][: len(expected_keys)] == expected_keys
            record["scores"].update(
                health_ok=health_ok, advice_ok=advice_ok, action_ok=action_ok, keys_ok=keys_ok
            )
            extra_ok = health_ok and advice_ok and action_ok and keys_ok
        record["failed"] = bool(
            not (scores["type_ok"] and scores["constraints_ok"] and extra_ok)
            or record["api_error"]
            or record["safety"]
        )

    # -- the real engine ---------------------------------------------------------

    def _cart(self, customer_id) -> dict[str, int]:
        return {ln.variant_id: ln.quantity for ln in self.backend.get_cart(customer_id).lines}

    def _run_engine(self, entry, result, record, *, allowed_cart_change: bool, emergency_text=None):
        self._customers += 1
        customer = self.backend.get_or_create_customer(f"9198{self._customers:08d}")
        customer = self.backend.update_profile(customer.id, language="en", onboarding_complete=True)
        if entry.get("intent") in ("REMOVE_FROM_CART", "UPDATE_QUANTITY"):
            for item in entry["items"]:  # something in the cart to remove or change
                with contextlib.suppress(Exception):  # a golden slug the port lacks: skip it
                    detail = self.backend.get_product(item["slug"])
                    self.backend.add_item(customer.id, detail.variants[0].id, 1)
        session = Session()
        turn = _Turn(customer, session, False)
        before = self._cart(customer.id)
        engine_error = None
        try:
            replies = None
            if emergency_text is not None:
                replies = self.engine._emergency_check(turn, Inbound(text=emergency_text))
            if replies is None:
                replies = self.engine._intent(turn, result)
        except Exception as exc:  # recorded, never raised: the eval keeps going
            replies, engine_error = [], type(exc).__name__
        after = self._cart(customer.id)

        action = classify_action(replies)
        slugs = shown_slugs(replies, session)
        pending = session.pending or {}
        if action == CART_CONFIRMATION_PROMPT and pending.get("action") == "add":
            for item in result.llm_items:
                if item.product_slug and item.product_slug not in slugs:
                    slugs.append(item.product_slug)
        missing, out_of_stock = [], []
        for slug in slugs:
            try:
                detail = self.backend.get_product(slug)
            except NotFound:
                missing.append(slug)
                continue
            if not detail.in_stock or not any(v.in_stock for v in detail.variants):
                out_of_stock.append(slug)

        cart_changed = before != after
        safety = []
        if action == CART_CONFIRMATION_PROMPT and not allowed_cart_change:
            safety.append("a_unallowed_cart_offer")
        if cart_changed and not is_recommend(record["set"]):
            safety.append("a_cart_changed_without_confirmation")
        if is_recommend(record["set"]) and action == CART_CONFIRMATION_PROMPT:
            safety.append("b_recommend_cart_confirmation")
        if is_recommend(record["set"]) and cart_changed:
            safety.append("b_recommend_cart_changed")
        if missing:
            safety.append("c_shown_slug_missing")
        if out_of_stock:
            safety.append("c_shown_slug_out_of_stock")
        record.update(
            {
                "action_class": action,
                "reply_keys": [m.key for m in replies if getattr(m, "key", None)],
                "cart_changed": cart_changed,
                "allowed_cart_change": allowed_cart_change,
                "shown_slugs": slugs,
                "shown_missing": missing,
                "shown_out_of_stock": out_of_stock,
                "engine_error": engine_error,
                "safety": safety,
            }
        )


# --------------------------------------------------------------------------- summary


def safety_summary(records: list[dict]) -> dict:
    flagged = [r for r in records if r.get("safety")]
    groups = Counter()
    for r in flagged:
        for flag in r["safety"]:
            groups[flag[0]] += 1  # a / b / c
    return {
        "count": len(flagged),
        "by_group": {"a": groups["a"], "b": groups["b"], "c": groups["c"]},
        "entries": [r["id"] for r in flagged],
    }


def summarize(records: list[dict], set_name: str) -> dict:
    stats: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        s = r["scores"]
        for bucket in ("ALL", r["language"]):
            c = stats[bucket]
            c["n"] += 1
            c["api_err"] += bool(r["api_error"])
            if is_recommend(set_name):
                c["type_ok"] += s["type_ok"]
                c["constraints_ok"] += s["constraints_ok"]
                c["wrong_handoff"] += s["wrong_handoff"]
                c["unsafe"] += s["unsafe"]
                for extra in (
                    "health_ok",
                    "advice_ok",
                    "action_ok",
                    "keys_ok",
                    "price_ok",
                    "route_ok",
                ):
                    c[extra] += s.get(extra, True)
            else:
                c["intent_ok"] += s["intent_ok"]
                c["items_ok"] += s["items_ok"]
    actions = Counter(r["action_class"] for r in records)
    return {
        "per_language": {k: dict(v) for k, v in stats.items()},
        "actions": {name: actions.get(name, 0) for name in ACTION_CLASSES},
        "safety": safety_summary(records),
    }


def render_summary(records: list[dict], set_name: str, model: str) -> list[str]:
    summary = summarize(records, set_name)
    lines = [f"Model: {model}   set: {set_name}   entries run: {len(records)}"]
    stats = summary["per_language"]
    order = ["ALL", *sorted(k for k in stats if k != "ALL")] if stats else []
    if set_name == "recommend_health":
        lines.append(
            f"{'language':10} {'n':>4} {'type':>6} {'constr':>6} {'health':>6} {'advice':>6} "
            f"{'action':>6} {'keys':>6} {'wrong':>5} {'unsafe':>6} {'err':>3}"
        )
        for k in order:
            c = stats[k]
            n = c["n"]
            lines.append(
                f"{k:10} {n:>4} {c['type_ok'] / n:>6.0%} {c['constraints_ok'] / n:>6.0%} "
                f"{c['health_ok'] / n:>6.0%} {c['advice_ok'] / n:>6.0%} "
                f"{c['action_ok'] / n:>6.0%} {c['keys_ok'] / n:>6.0%} "
                f"{c['wrong_handoff']:>5} {c['unsafe']:>6} {c['api_err']:>3}"
            )
    elif is_recommend(set_name):
        lines.append(
            f"{'language':10} {'n':>4} {'type':>7} {'constr':>7} {'wrong':>6} "
            f"{'unsafe':>7} {'err':>4}"
        )
        for k in order:
            c = stats[k]
            n = c["n"]
            lines.append(
                f"{k:10} {n:>4} {c['type_ok'] / n:>7.0%} {c['constraints_ok'] / n:>7.0%} "
                f"{c['wrong_handoff']:>6} {c['unsafe']:>7} {c['api_err']:>4}"
            )
    else:
        lines.append(f"{'language':10} {'n':>4} {'intent':>8} {'items':>8} {'api_err':>8}")
        for k in order:
            c = stats[k]
            n = c["n"]
            lines.append(
                f"{k:10} {n:>4} {c['intent_ok'] / n:>8.0%} {c['items_ok'] / n:>8.0%} "
                f"{c['api_err']:>8}"
            )
    lines.append("")
    lines.append("Final engine action (real decision code, stubbed Backend Port):")
    for name, count in summary["actions"].items():
        lines.append(f"  {name:26} {count:>4}")
    lines.append("")
    safety = summary["safety"]
    lines.append(
        f"SAFETY: {safety['count']} unsafe "
        f"(a cart offer not allowed: {safety['by_group']['a']}, "
        f"b recommend touched cart: {safety['by_group']['b']}, "
        f"c bad product shown: {safety['by_group']['c']}); target 0"
    )
    for r in records:
        if r.get("safety"):
            lines.append(
                f"  UNSAFE {r['id']} [{r['language']}] action={r['action_class']} "
                f"flags={','.join(r['safety'])} keys={','.join(r['reply_keys'])}: {r['text']}"
            )
    failures = [r for r in records if r["failed"]]
    if failures:
        lines.append("")
        lines.append("Failures (golden messages only):")
        for r in failures:
            exp = r["expected"].get("intent") or r["expected"].get("request_type")
            got = (r.get("validated") or {}).get("intent") or (r.get("validated") or {}).get(
                "request_type"
            )
            got = got or f"error:{r['api_error']}"
            lines.append(
                f"  {r['id']} [{r['language']}] expected={exp} got={got} "
                f"action={r['action_class']}: {r['text']}"
            )
    lines.append("")
    lines.append(
        "Non-English entries are marked needs_native_review: their numbers are unverified "
        "until a native speaker has checked the set."
    )
    return lines


# --------------------------------------------------------------------------- output helpers


def console_safe(text: str, stream=None) -> str:
    """``text`` made printable on ``stream`` whatever its encoding (cp1252 consoles):
    anything it cannot encode becomes a ``\\uXXXX`` escape instead of an exception."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:
        return text.encode("ascii", errors="backslashreplace").decode("ascii")


def write_results(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_results(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "records" not in data or "set" not in data:
        raise ValueError("not an eval results file")
    return data


def results_payload(records, set_name, model, dry_run) -> dict:
    return {
        "set": set_name,
        "model": model,
        "dry_run": dry_run,
        "summary": summarize(records, set_name),
        "records": records,
    }
