"""Conversation engine: one inbound message in, channel-neutral replies out.

    engine.handle(whatsapp_id, Inbound) -> list[Outbound]

Everything the customer is told about products, prices, stock, cart, orders, payment,
ETA and tracking comes from the Backend Port in the same turn; nothing is invented.
The intent layer (``apps.ai``) supplies raw entity text; the engine resolves it to real
products and categories only through the port. ``handle`` never raises: backend and
programming errors become friendly localized messages, and a failed turn does not save
the session, so the customer can simply try again.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from apps.ai import llm_state
from apps.ai.classifier import CatalogEntry, redact
from apps.ai.intents import Entities, Intent, IntentResult, LLMItem, Stage, build_reply_id
from apps.ai.llm_fallback import LLMContext
from apps.ai.pipeline import IntentPipeline, pipeline_from_settings
from apps.ai.text import normalize
from apps.iva_common.money import format_money

from .adapters import get_backend
from .address_check import looks_like_address
from .backend_port import (
    DIETS,
    MAX_PAGE_SIZE,
    BackendError,
    BackendPort,
    BackendUnavailable,
    CartEmpty,
    CartView,
    Category,
    CheckoutRejected,
    CustomerRef,
    InvalidQuantity,
    NotAuthorized,
    NotFound,
    OrderStatusView,
    PaymentStatus,
    ProductSummary,
    ProductUnavailable,
)
from .catalog_order import PORT_MAX_QUANTITY, ValidatedOrder, effective_cap, validate_order
from .channel import (
    MAX_CAROUSEL_CARDS,
    MAX_ROWS,
    Button,
    CarouselCard,
    Inbound,
    Outbound,
    OutButtons,
    OutCarousel,
    OutCTA,
    OutList,
    OutProductCard,
    OutText,
    Row,
)
from .messages import (
    CART_WORDS,
    DIET_WORDS,
    ESCAPE_WORDS,
    GREETING_WORDS,
    HELP_WORDS,
    LANGUAGE_WORDS,
    NO_WORDS,
    ORDER_STATUS_WORDS,
    YES_WORDS,
    t,
)
from .recommend_flow import RecommendFlow
from .state import Session, SessionStore, SessionStoreError, State, store_from_settings

logger = logging.getLogger(__name__)

_REDACTED_RE = re.compile(r"\[(?:link|email|upi|id|number)\]")


def scrub_query(query: str | None) -> str:
    """A search phrase without phone numbers, long digit runs, links, emails or handles: they
    never reach the Port, a stored listing, a log or a reply."""
    cleaned = _REDACTED_RE.sub(" ", redact(query or "", limit=1000))
    return " ".join(cleaned.split())


ACT = "ACT:"  # engine-owned reply IDs (diet, paging, addresses, confirm); never sent to apps.ai
PAGE_SIZE = MAX_ROWS - 1  # one row stays free for "More…"
CARD_PAGE_SIZE = 4  # products shown as cards (category browse, plain search results) per page
CAROUSEL_PAGE_SIZE = MAX_CAROUSEL_CARDS  # category page size when it can go out as a carousel
HINTS_TTL_SECONDS = 300
MAX_HINT_PRODUCTS = 500
MAX_MENU_GROUPS = 4
MENU_FIXED_ROWS = 5  # cart, orders, help, language, diet
NAME_MAX = 60
ADDRESS_MIN = 10
ADDRESS_MAX = 300
QUICK_QUANTITIES = (1, 2, 3)
LANGUAGE_BUTTONS = (("en", "English"), ("te", "తెలుగు"), ("hi", "हिंदी"))
_REF_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
# Suggestions a customer can confirm with one tap (they need no entity).
_CONFIRMABLE = (
    Intent.VIEW_CART,
    Intent.CHECKOUT,
    Intent.ORDER_STATUS,
    Intent.PAYMENT_STATUS,
    Intent.TRACKING,
    Intent.HELP,
    Intent.CATEGORY_SELECT,
    Intent.LANGUAGE_SELECT,
)
_KNOWN_PAYMENT = {s.value for s in PaymentStatus}
_LLM_CART_INTENTS = (Intent.ADD_TO_CART, Intent.REMOVE_FROM_CART, Intent.UPDATE_QUANTITY)


class _Turn:
    """Per-message context."""

    def __init__(self, customer: CustomerRef, session: Session, fresh: bool):
        self.customer = customer
        self.session = session
        self.fresh = fresh

    @property
    def lang(self) -> str:
        return self.customer.language or "en"


def _fold(text: str) -> str:
    """Normalize and strip accents ("Café" -> "cafe") for matching names."""
    decomposed = unicodedata.normalize("NFKD", normalize(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _words_match(query: str, name: str) -> bool:
    """Every query word starts some word of ``name`` (plural-insensitive)."""
    query_words = [_singular(w) for w in _fold(query).split()]
    name_words = [_singular(w) for w in _fold(name).split()]
    return bool(query_words) and all(any(n.startswith(q) for n in name_words) for q in query_words)


def _sanitize_contact_name(raw: str | None) -> str:
    """Trim, collapse whitespace, strip control/format characters, cap the length.

    Never raises and never guesses: an unusable name (``None``, empty, all control
    characters) simply becomes ``""`` so the caller can greet without a name.
    """
    if not raw:
        return ""
    stripped = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    collapsed = " ".join(stripped.split())
    return collapsed[:NAME_MAX]


@dataclass(frozen=True)
class _Found:
    page: list[ProductSummary]
    total: int
    query: str  # the query that matched (the whole text or one of its words)
    pageable: bool
    hidden: int  # matches hidden by the saved diet


class ConversationEngine(RecommendFlow):
    def __init__(
        self,
        backend: BackendPort | None = None,
        store: SessionStore | None = None,
        pipeline: IntentPipeline | None = None,
        *,
        hints_ttl: float = HINTS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._backend = backend
        self.store = store if store is not None else store_from_settings()
        self.pipeline = pipeline if pipeline is not None else pipeline_from_settings()
        self._hints_ttl = hints_ttl
        self._clock = clock
        self._hints_cache: tuple[float, tuple[str, ...], tuple[str, ...]] | None = None
        self._llm_catalog_cache: tuple[float, tuple[CatalogEntry, ...]] | None = None
        self._code_categories_cache: tuple[float, tuple[tuple[str, str], ...]] | None = None

    @property
    def backend(self) -> BackendPort:
        return self._backend if self._backend is not None else get_backend()

    # ------------------------------------------------------------------ entry point

    def handle(self, whatsapp_id: str, inbound: Inbound) -> list[Outbound]:
        if (
            not isinstance(inbound, Inbound)
            or not isinstance(whatsapp_id, str)
            or not whatsapp_id.strip()
        ):
            logger.error("engine: invalid inbound message")
            return [OutText(t(None, "error.generic"), "error.generic")]
        try:
            customer = self.backend.get_or_create_customer(whatsapp_id)
        except Exception as exc:
            return self._error_reply(None, exc)

        if not customer.name:
            # Never overwrite a saved name; the sanitized value itself is never logged.
            name = _sanitize_contact_name(inbound.profile_name)
            if name:
                try:
                    customer = self.backend.update_profile(customer.id, name=name)
                except Exception as exc:
                    return self._error_reply(None, exc)

        session, fresh = self._load(whatsapp_id)
        turn = _Turn(customer, session, fresh)
        try:
            replies = self._dispatch(turn, inbound)
        except Exception as exc:
            return self._error_reply(turn, exc)  # session not saved: previous state kept
        self._save(whatsapp_id, turn.session)
        return replies

    def rate_limited_reply(self, whatsapp_id: str) -> list[Outbound]:
        """One polite localized notice for a customer over the per-minute message cap.

        Unlike ``handle``, this never loads or saves the session and never runs the
        intent pipeline: the message that triggered it is not otherwise processed.
        """
        try:
            lang = self.backend.get_or_create_customer(whatsapp_id).language
        except Exception:
            lang = None
        return [OutText(t(lang, "limit.rate_limited"), "limit.rate_limited")]

    def _load(self, whatsapp_id: str) -> tuple[Session, bool]:
        try:
            session = self.store.load(whatsapp_id)
        except SessionStoreError:
            logger.warning("engine: session store unavailable on load")
            session = None
        return (session, False) if session is not None else (Session(), True)

    def _save(self, whatsapp_id: str, session: Session) -> None:
        try:
            self.store.save(whatsapp_id, session)
        except SessionStoreError:
            logger.warning("engine: session store unavailable on save")

    def _error_reply(self, c: _Turn | None, exc: Exception) -> list[Outbound]:
        lang = c.lang if c else None
        menu = (Button(Intent.CATEGORY_SELECT.value, t(lang, "btn.menu")),)
        if isinstance(exc, BackendUnavailable):
            logger.warning("engine: backend unavailable")
            return [OutButtons(t(lang, "error.unavailable"), menu, "error.unavailable")]
        if isinstance(exc, InvalidQuantity):
            return [OutButtons(t(lang, "qty.invalid"), menu, "qty.invalid")]
        if isinstance(exc, ProductUnavailable):
            return [OutButtons(t(lang, "error.out_of_stock"), menu, "error.out_of_stock")]
        if isinstance(exc, CartEmpty):
            return [OutButtons(t(lang, "cart.empty"), menu, "cart.empty")]
        if isinstance(exc, NotFound | NotAuthorized):
            return [OutButtons(t(lang, "error.not_found"), menu, "error.not_found")]
        if isinstance(exc, CheckoutRejected):
            return [OutButtons(t(lang, "checkout.rejected"), menu, "checkout.rejected")]
        if isinstance(exc, BackendError):
            logger.error("engine: backend error=%s", type(exc).__name__)
        elif isinstance(exc, ValueError):
            logger.error("engine: port rejected an argument (bug) error=%s", type(exc).__name__)
        else:
            logger.error("engine: unexpected error=%s", type(exc).__name__)
        return [OutText(t(lang, "error.generic"), "error.generic")]

    # ------------------------------------------------------------------ helpers

    def _text(self, c: _Turn | None, key: str, **values) -> OutText:
        return OutText(t(c.lang if c else None, key, **values), key)

    def _named_text(self, c: _Turn, key: str) -> OutText:
        """``key`` with the customer's name, or ``key_noname`` (no stray space or comma) when the
        name is missing."""
        name = (c.customer.name or "").strip()
        if not name:
            return self._text(c, f"{key}_noname")
        return self._text(c, key, name=name)

    def _button(self, c: _Turn, reply_id: str, key: str) -> Button:
        return Button(reply_id, t(c.lang, key))

    def _menu_button(self, c: _Turn) -> Button:
        return self._button(c, Intent.CATEGORY_SELECT.value, "btn.menu")

    def _ref(self, c: _Turn, real_id: str) -> str:
        """A reply-ID-safe reference for a backend ID (short token if it does not fit)."""
        if _REF_RE.match(real_id):
            return real_id
        token = "r" + hashlib.sha256(real_id.encode("utf-8")).hexdigest()[:12]
        c.session.remember_ref(token, real_id)
        return token

    def _unref(self, c: _Turn, ref: str | None) -> str | None:
        return c.session.refs.get(ref, ref) if ref else ref

    def _reply(self, c: _Turn, intent: Intent, real_id: str | None = None, qty=None) -> str:
        ref = self._ref(c, real_id) if real_id is not None else None
        return build_reply_id(intent, ref, qty)

    def _money(self, amount, currency: str) -> str:
        return format_money(amount, currency)

    def _status_name(self, c: _Turn, kind: str, value: str) -> str:
        key = f"status.{kind}.{value}"
        try:
            return t(c.lang, key)
        except KeyError:
            return value  # a status the channel does not know yet: show it as the port says

    def _all_categories(self) -> list[Category]:
        groups = self.backend.list_categories(None)
        result = list(groups)
        for group in groups:
            result.extend(self.backend.list_categories(group.id))
        return result

    def _hints(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Catalog names for the intent layer, cached briefly. Empty if the port fails."""
        now = self._clock()
        if self._hints_cache and now < self._hints_cache[0]:
            return self._hints_cache[1], self._hints_cache[2]
        try:
            categories = tuple(c.name for c in self._all_categories())
            products: list[str] = []
            offset = 0
            while len(products) < MAX_HINT_PRODUCTS:
                page, total = self.backend.list_products(limit=MAX_PAGE_SIZE, offset=offset)
                products.extend(p.name for p in page)
                offset += len(page)
                if not page or offset >= total:
                    break
        except Exception as exc:
            logger.warning("engine: catalog hints unavailable error=%s", type(exc).__name__)
            return (), ()
        self._hints_cache = (now + self._hints_ttl, tuple(products), categories)
        return self._hints_cache[1], self._hints_cache[2]

    def _code_categories(self) -> tuple[tuple[str, str], ...]:
        """The Port's ``(slug, name)`` categories for the code-only routes (cached briefly):
        a category word in a message restricts picks whether or not a model is available."""
        now = self._clock()
        if self._code_categories_cache and now < self._code_categories_cache[0]:
            return self._code_categories_cache[1]
        cats = self._llm_categories()
        if cats:
            self._code_categories_cache = (now + self._hints_ttl, cats)
        return cats

    def _understand(self, inbound: Inbound, llm_context: LLMContext | None = None) -> IntentResult:
        products, categories = self._hints()
        text = inbound.text if inbound.text is not None else inbound.reply_title
        return self.pipeline.understand(
            text,
            reply_id=inbound.reply_id,
            product_names=products,
            category_names=categories,
            llm_context=llm_context,
            code_categories=self._code_categories(),
        )

    # States in which free text may go to the LLM fallback. Everything else (language
    # and diet selection, address entry, order confirmation, payment) is handled by
    # fixed rules only.
    _LLM_STATES = frozenset(
        {State.NEW, State.MAIN_MENU, State.GROUP, State.CATEGORY, State.PRODUCT, State.CART}
    )

    def _llm_catalog(self) -> tuple[CatalogEntry, ...]:
        """Compact catalog allow-list (slug = port product ID, name, category), cached
        briefly. Empty if the port fails (then the LLM simply is not used)."""
        now = self._clock()
        if self._llm_catalog_cache and now < self._llm_catalog_cache[0]:
            return self._llm_catalog_cache[1]
        try:
            names = {cat.id: cat.name for cat in self._all_categories()}
            entries: list[CatalogEntry] = []
            offset = 0
            while len(entries) < MAX_HINT_PRODUCTS:
                page, total = self.backend.list_products(limit=MAX_PAGE_SIZE, offset=offset)
                entries.extend(
                    CatalogEntry(p.id, p.name, names.get(p.category_id, "")) for p in page
                )
                offset += len(page)
                if not page or offset >= total:
                    break
        except Exception as exc:
            logger.warning("engine: llm catalog unavailable error=%s", type(exc).__name__)
            return ()
        self._llm_catalog_cache = (now + self._hints_ttl, tuple(entries))
        return self._llm_catalog_cache[1]

    def _llm_context(self, c: _Turn, inbound: Inbound) -> LLMContext | None:
        """The context that lets the pipeline call the LLM, or ``None`` when it must not:
        feature off, a button/list tap, an unfinished flow state, or a pending question."""
        if not getattr(self.pipeline, "llm_fallback_enabled", False):
            return None  # the feature is off: nothing to explain
        if inbound.reply_id or not (inbound.text or "").strip():
            return None  # a tap or an empty message never goes to the model
        if c.session.state not in self._LLM_STATES:
            logger.info("engine: llm not used reason=state")
            return None
        if c.session.pending is not None:
            logger.info("engine: llm not used reason=pending_question")
            return None
        catalog = self._llm_catalog()
        if not catalog:
            logger.info("engine: llm not used reason=no_catalog")
            return None
        return LLMContext(
            catalog, c.customer.id, PORT_MAX_QUANTITY, categories=self._llm_categories()
        )

    def _llm_categories(self) -> tuple[tuple[str, str], ...]:
        try:
            return tuple((cat.id, cat.name) for cat in self._all_categories())
        except Exception as exc:
            logger.warning("engine: llm categories unavailable error=%s", type(exc).__name__)
            return ()

    # ------------------------------------------------------------------ dispatch

    def _dispatch(self, c: _Turn, inbound: Inbound) -> list[Outbound]:
        if inbound.order is not None:
            return self._catalog_order(c, inbound.order)
        emergency = self._emergency_check(c, inbound)
        if emergency is not None:
            return emergency
        if not c.customer.onboarding_complete:
            return self._onboarding(c, inbound)
        if inbound.reply_id and inbound.reply_id.startswith(ACT):
            return self._action(c, inbound.reply_id[len(ACT) :])

        text = (inbound.text or "").strip()
        state = c.session.state
        if text and state is State.ONBOARD_DIET:  # changing the preference later
            diet = self._diet_from_text(text)
            if diet:
                return self._set_diet(c, diet)
        if text and state is State.CHECKOUT_ADDRESS:
            folded = normalize(text)
            if folded in ESCAPE_WORDS:
                return self._main_menu(c)
            command = self._checkout_command(c, folded)
            if command is not None:
                return command
            return self._address_typed(c, text)

        if text and c.session.pending is None and state in self._LLM_STATES and self._yes_no(text):
            return self._stale_answer(
                c
            )  # a Yes/No with nothing waiting: never a guess, never a model

        result = self._understand(inbound, self._llm_context(c, inbound))
        logger.info("engine: intent=%s stage=%s state=%s", result.intent, result.stage, state)
        replies = self._intent(c, result)
        if result.notes:
            replies = [*(self._text(c, key) for key in result.notes), *replies]
        return replies

    @staticmethod
    def _yes_no(text: str) -> bool:
        folded = normalize(text)
        return folded in YES_WORDS or folded in NO_WORDS

    def _stale_answer(self, c: _Turn) -> list[Outbound]:
        """A Yes/No (button or typed) for a question that is no longer waiting: one neutral
        reply, nothing changes, no model call."""
        key = "stale.expired"
        return [OutButtons(t(c.lang, key), (self._menu_button(c),), key)]

    def _intent(self, c: _Turn, r: IntentResult) -> list[Outbound]:
        s = c.session
        e = r.entities
        pending, s.pending = s.pending, None
        if pending and pending.get("kind") == "llm_confirm":
            llm_state.stat("cart_confirm.declined")  # the customer moved on instead of answering
        intent = r.intent
        if r.request is not None:  # grounded recommendation flow (read-only)
            return self._recommendation(c, r.request)

        if c.fresh and intent in (Intent.GREETING, Intent.FALLBACK, Intent.CLARIFY):
            return [self._named_text(c, "welcome_back"), *self._main_menu(c)]

        if intent is Intent.GREETING:
            return [self._named_text(c, "greeting"), *self._main_menu(c)]
        if intent is Intent.LANGUAGE_SELECT:
            if e.language_choice:
                c.customer = self.backend.update_profile(c.customer.id, language=e.language_choice)
                return [self._text(c, "language.changed"), *self._main_menu(c)]
            s.state = State.LANGUAGE
            return [self._language_prompt(c)]
        if intent is Intent.HELP:
            buttons = (self._menu_button(c), self._button(c, Intent.VIEW_CART.value, "btn.cart"))
            return [OutButtons(t(c.lang, "help"), buttons, "help")]
        if intent is Intent.VIEW_CART:
            return self._show_cart(c)
        if intent is Intent.CHECKOUT:
            return self._start_checkout(c)
        if intent in (Intent.ORDER_STATUS, Intent.TRACKING):
            return self._order_status(c, self._unref(c, e.order_number))
        if intent is Intent.PAYMENT_STATUS:
            return self._payment_status(c, self._unref(c, e.order_number))
        if intent is Intent.CATEGORY_SELECT:
            if e.reference_id:
                return self._open_category(c, self._unref(c, e.reference_id))
            if e.category_query:
                return self._find_category(c, e.category_query)
            return self._main_menu(c)
        if intent is Intent.PRODUCT_SEARCH:
            if e.product_query:
                return self._search(c, e.product_query)
            if e.category_query:
                return self._find_category(c, e.category_query)
            return self._ask_product(c)
        if intent is Intent.PRODUCT_SELECT:
            quantity = pending.get("quantity") if pending and pending["kind"] == "choose" else None
            if e.reference_id:
                return self._select_product(c, self._unref(c, e.reference_id), quantity)
            if e.choice_index:
                ids = (s.listing or {}).get("ids") or []
                if 1 <= e.choice_index <= len(ids):
                    return self._select_product(c, ids[e.choice_index - 1], quantity)
                return self._ask_product(c)
            if e.product_query:
                return self._search(c, e.product_query)
            return self._ask_product(c)
        if (
            r.stage is Stage.FUZZY
            and intent in _LLM_CART_INTENTS
            and getattr(settings, "IVA_FUZZY_CART_CONFIRM", True)
        ):
            confirm = self._fuzzy_cart_confirmation(c, r)
            if confirm is not None:
                logger.info("engine: stage=fuzzy outcome=cart_confirmation intent=%s", intent)
                return confirm
        if r.stage is Stage.LLM and intent in _LLM_CART_INTENTS and r.llm_items:
            confirm = self._llm_cart_confirmation(c, r)
            if confirm is not None:
                return confirm
        if intent is Intent.ADD_TO_CART:
            if r.stage is Stage.LLM and not e.reference_id and e.product_query:
                # An LLM-derived add with no usable catalog slug: browse (read-only), never add.
                return self._search(c, e.product_query)
            if e.reference_id:
                variant_id = self._unref(c, e.reference_id)
                if e.quantity is not None:
                    return self._add(c, variant_id, e.quantity)
                return self._ask_quantity(c, {"kind": "add", "variant_id": variant_id})
            if e.product_query:
                return self._search(c, e.product_query, quantity=e.quantity, adding=True)
            if pending and pending["kind"] == "add":
                if e.quantity is not None:
                    return self._add(c, pending["variant_id"], e.quantity)
                return self._ask_quantity(c, pending)
            return self._ask_product(c)
        if intent is Intent.REMOVE_FROM_CART:
            if e.reference_id:
                return self._remove_line(c, self._unref(c, e.reference_id))
            return self._pick_line(c, "remove", e.product_query, e.choice_index, None)
        if intent is Intent.UPDATE_QUANTITY:
            if e.reference_id:
                line_id = self._unref(c, e.reference_id)
                if e.quantity is not None:
                    return self._update_line(c, line_id, e.quantity)
                return self._ask_line_quantity(c, line_id)
            if pending and pending["kind"] == "update" and e.quantity is not None:
                if not e.product_query:
                    return self._update_line(c, pending["line_id"], e.quantity)
            return self._pick_line(c, "update", e.product_query, e.choice_index, e.quantity)
        if intent is Intent.PLAN_RECEIVED:
            return self._plan_received(c, e)
        if intent is Intent.CLARIFY:
            s.pending = pending  # still waiting for whatever we asked before
            return self._clarify(c, r.suggested_intent)

        # FALLBACK: a bare quantity completes what we were waiting for.
        if pending and pending["kind"] in ("add", "update"):
            if e.quantity is not None:
                if pending["kind"] == "add":
                    return self._add(c, pending["variant_id"], e.quantity)
                return self._update_line(c, pending["line_id"], e.quantity)
            s.pending = pending
            return [self._text(c, "qty.ask")]
        return [self._fallback(c)]

    # ------------------------------------------------------------------ onboarding

    def _language_prompt(self, c: _Turn, *, greet: bool = False) -> OutButtons:
        buttons = tuple(
            Button(build_reply_id(Intent.LANGUAGE_SELECT, code), title)
            for code, title in LANGUAGE_BUTTONS
        )
        if greet:
            name = c.customer.name
            key = "language.prompt_greeting_named" if name else "language.prompt_greeting_unnamed"
            body = t(c.lang, key, name=name or "")
        else:
            key = "language.prompt"
            body = t(c.lang, key)
        return OutButtons(body, buttons, key)

    def _onboarding(self, c: _Turn, inbound: Inbound) -> list[Outbound]:
        s = c.session
        if c.customer.language is None:
            result = self._understand(inbound)
            choice = (
                result.entities.language_choice if result.intent is Intent.LANGUAGE_SELECT else None
            )
            if not choice:
                # The greeting-by-name line belongs only on the first time we ask in
                # this session, never on a re-prompt after unclear text.
                greet = s.state is not State.LANGUAGE
                s.state = State.LANGUAGE
                return [self._language_prompt(c, greet=greet)]
            c.customer = self.backend.update_profile(c.customer.id, language=choice)
            # the first choice is confirmed, in the chosen language, before the welcome
            return [self._text(c, "language.changed"), *self._finish_onboarding(c)]
        return self._finish_onboarding(c)

    def _diet_prompt(self, c: _Turn) -> list[Outbound]:
        rows = tuple(
            Row(f"{ACT}diet:{choice}", t(c.lang, f"diet.{choice}")) for choice in (*DIETS, "skip")
        )
        return [OutList(t(c.lang, "diet.ask"), t(c.lang, "list.choose"), rows, "diet.ask")]

    @staticmethod
    def _diet_from_text(text: str) -> str | None:
        folded = normalize(text)
        for diet, words in DIET_WORDS.items():
            if folded in words:
                return diet
        return None

    def _diet_choice(self, inbound: Inbound) -> str | None:
        if inbound.reply_id and inbound.reply_id.startswith(f"{ACT}diet:"):
            choice = inbound.reply_id.split(":", 2)[2]
            return choice if choice in (*DIETS, "skip") else None
        return self._diet_from_text(inbound.text or "")

    def _finish_onboarding(self, c: _Turn) -> list[Outbound]:
        c.customer = self.backend.update_profile(c.customer.id, onboarding_complete=True)
        c.session.state = State.MAIN_MENU
        return self._welcome(c)

    def _planner_url(self) -> str | None:
        url = getattr(settings, "IVA_PLANNER_URL", "") or ""
        parsed = urlparse(url)
        return url if parsed.scheme == "https" and parsed.netloc else None

    def _website_url(self) -> str | None:
        url = getattr(settings, "IVA_WEBSITE_SHOP_URL", "") or ""
        parsed = urlparse(url)
        return url if parsed.scheme == "https" and parsed.netloc else None

    def _send_website_cta(self, c: _Turn) -> list[Outbound]:
        url = self._website_url()
        if not url:
            return self._main_menu(c)
        body = t(c.lang, "website.cta_body")
        return [OutCTA(body, t(c.lang, "btn.website"), url, "website.cta_body")]

    def _browse_menu_button(self, c: _Turn) -> Button:
        return self._button(c, Intent.CATEGORY_SELECT.value, "btn.browse_menu")

    def _welcome(self, c: _Turn) -> list[Outbound]:
        url = self._planner_url()
        if not url:
            return self._main_menu(c)
        # No name greeting here: the very first reply (the language prompt) already
        # greeted the customer by name, if it had one.
        key = "welcome.planner_intro"
        cta = OutCTA(t(c.lang, key), t(c.lang, "btn.planner"), url, key)
        browse = OutButtons(
            t(c.lang, "welcome.browse_prompt"), (self._browse_menu_button(c),),
            "welcome.browse_prompt",
        )  # fmt: skip
        return [cta, browse]

    def _resend_planner_cta(self, c: _Turn) -> list[Outbound]:
        url = self._planner_url()
        if not url:
            return self._main_menu(c)
        body = t(c.lang, "planner.cta_body")
        return [OutCTA(body, t(c.lang, "btn.planner"), url, "planner.cta_body")]

    def _set_diet(self, c: _Turn, choice: str) -> list[Outbound]:
        if choice == "skip":  # changing later: skip keeps the current preference
            return self._main_menu(c)
        c.customer = self.backend.update_profile(c.customer.id, diet=choice)
        return [self._text(c, "diet.saved", diet=t(c.lang, f"diet.{choice}")), *self._main_menu(c)]

    # ------------------------------------------------------------------ engine actions

    def _action(self, c: _Turn, action: str) -> list[Outbound]:
        name, _, arg = action.partition(":")
        s = c.session
        if name == "diet":
            if not arg:
                s.state = State.ONBOARD_DIET
                return self._diet_prompt(c)
            if arg in (*DIETS, "skip"):
                return self._set_diet(c, arg)
        elif name == "more":
            return self._more(c)
        elif name == "showall":
            return self._more(c, restart_all=True)
        elif name == "continue":
            return self._continue_shopping(c)
        elif name == "addcard" and arg:
            return self._card_add_to_cart(c, self._unref(c, arg))
        elif name == "ordernow" and arg:
            return self._card_order_now(c, self._unref(c, arg))
        elif name == "addr" and arg:
            s.address_id = self._unref(c, arg)
            return self._confirm(c)
        elif name == "resume":  # "Continue checkout" after a greeting mid-checkout
            return self._start_checkout(c)
        elif name == "newaddr":
            s.state = State.CHECKOUT_ADDRESS
            s.address_id = None
            return [OutButtons(t(c.lang, "checkout.type_address"), (self._menu_button(c),),
                               "checkout.type_address")]  # fmt: skip
        elif name == "confirm":
            return self._place_order(c)
        elif name == "cancel":
            s.address_id = None
            return [self._text(c, "checkout.cancelled"), *self._main_menu(c)]
        elif name == "edit":
            return self._edit_cart(c)
        elif name == "line" and arg:
            return self._line_actions(c, self._unref(c, arg))
        elif name == "plan":
            return self._resend_planner_cta(c)
        elif name == "website":
            return self._send_website_cta(c)
        elif name == "showcard" and arg:
            return self._show_card(c, self._unref(c, arg))
        elif name == "planadd":
            return self._add_all_plan_picks(c)
        elif name == "llmyes":
            return self._llm_apply(c)
        elif name == "llmno":
            if not s.pending or s.pending.get("kind") != "llm_confirm":
                s.pending = None
                return self._stale_answer(c)  # an answered, expired or superseded question
            s.pending = None
            llm_state.stat("cart_confirm.declined")
            buttons = (self._button(c, Intent.VIEW_CART.value, "btn.cart"), self._menu_button(c))
            return [OutButtons(t(c.lang, "llm.cancelled"), buttons, "llm.cancelled")]
        return [self._fallback(c)]

    # ------------------------------------------------------------------ menu & browsing

    def _main_menu(self, c: _Turn) -> list[Outbound]:
        s = c.session
        s.state = State.MAIN_MENU
        s.listing = None
        # 5 fixed rows + up to 2 optional link rows; category groups take what is left of
        # WhatsApp's 10-row limit.
        links = [
            Row(f"{ACT}{act}", t(c.lang, key))
            for act, key, url in (
                ("plan", "btn.planner", self._planner_url()),
                ("website", "menu.website", self._website_url()),
            )
            if url
        ]
        group_slots = min(MAX_MENU_GROUPS, MAX_ROWS - MENU_FIXED_ROWS - len(links))
        groups = self.backend.list_categories(None)[:group_slots]
        rows = [Row(self._reply(c, Intent.CATEGORY_SELECT, g.id), g.name) for g in groups]
        rows += [
            Row(Intent.VIEW_CART.value, t(c.lang, "menu.cart")),
            Row(Intent.ORDER_STATUS.value, t(c.lang, "menu.orders")),
            Row(Intent.HELP.value, t(c.lang, "menu.help")),
            Row(Intent.LANGUAGE_SELECT.value, t(c.lang, "menu.language")),
            Row(f"{ACT}diet", t(c.lang, "menu.diet")),
        ]
        rows += links
        return [OutList(t(c.lang, "menu.body"), t(c.lang, "menu.button"), tuple(rows), "menu.body")]

    def _fallback(self, c: _Turn) -> OutButtons:
        buttons = (self._menu_button(c), self._button(c, Intent.HELP.value, "btn.help"))
        body = f"{t(c.lang, 'fallback')}\n{t(c.lang, 'fallback.hint')}"
        return OutButtons(body, buttons, "fallback")

    def _ask_product(self, c: _Turn) -> list[Outbound]:
        return [OutButtons(t(c.lang, "ask.product"), (self._menu_button(c),), "ask.product")]

    def _clarify(self, c: _Turn, suggested: Intent | None) -> list[Outbound]:
        if suggested is None or suggested in (Intent.FALLBACK, Intent.CLARIFY):
            return [self._fallback(c)]
        key = f"clarify.{suggested.value}"
        buttons = []
        if suggested in _CONFIRMABLE:
            buttons.append(self._button(c, suggested.value, "btn.yes"))
        if suggested is not Intent.CATEGORY_SELECT:
            buttons.append(self._menu_button(c))
        return [OutButtons(t(c.lang, key), tuple(buttons), key)]

    def _open_category(self, c: _Turn, category_id: str) -> list[Outbound]:
        categories = {cat.id: cat for cat in self._all_categories()}
        category = categories.get(category_id)
        if category is None:
            raise NotFound("Category not found.")
        if category.parent_id is None:
            return self._show_group(c, category, 0)
        return self._show_category(c, category, 0)

    def _find_category(self, c: _Turn, query: str) -> list[Outbound]:
        query = scrub_query(query)
        categories = self._all_categories()
        exact = [cat for cat in categories if _fold(cat.name) == _fold(query)]
        matches = exact or [cat for cat in categories if _words_match(query, cat.name)]
        if not matches:
            body = t(c.lang, "category.none")
            return [OutButtons(body, (self._menu_button(c),), "category.none")]
        if len(matches) == 1:
            return self._open_category(c, matches[0].id)
        rows = tuple(
            Row(self._reply(c, Intent.CATEGORY_SELECT, cat.id), cat.name)
            for cat in matches[:MAX_ROWS]
        )
        body = t(c.lang, "category.choose")
        return [OutList(body, t(c.lang, "list.choose"), rows, "category.choose")]

    def _show_group(self, c: _Turn, group: Category, offset: int) -> list[Outbound]:
        children = self.backend.list_categories(group.id)
        page = children[offset : offset + PAGE_SIZE]
        if not page:
            return self._main_menu(c)
        rows = [Row(self._reply(c, Intent.CATEGORY_SELECT, cat.id), cat.name) for cat in page]
        if offset + len(page) < len(children):
            rows.append(self._more_row(c))
        s = c.session
        s.state = State.GROUP
        s.listing = {"kind": "group", "group_id": group.id, "title": group.name, "offset": offset}
        body = t(c.lang, "group.body", group=group.name)
        return [OutList(body, t(c.lang, "list.choose"), tuple(rows), "group.body")]

    def _diet(self, c: _Turn, show_all: bool) -> str | None:
        return None if show_all else c.customer.diet

    def _unfiltered_total(self, **criteria) -> int:
        return self.backend.list_products(diet=None, limit=1, offset=0, **criteria)[1]

    def _diet_notice(self, c: _Turn, key: str, **values) -> OutButtons:
        """Say that the saved diet hid items and offer to show them or change the preference."""
        diet = t(c.lang, f"diet.{c.customer.diet}")
        buttons = (
            self._button(c, f"{ACT}showall", "btn.show_anyway"),
            self._button(c, f"{ACT}diet", "btn.change_diet"),
            self._menu_button(c),
        )
        return OutButtons(t(c.lang, key, diet=diet, **values), buttons, key)

    def _show_category(
        self, c: _Turn, category: Category, offset: int, show_all: bool = False
    ) -> list[Outbound]:
        diet = self._diet(c, show_all)
        page, total = self.backend.list_products(
            category_id=category.id, diet=diet, limit=CAROUSEL_PAGE_SIZE, offset=offset
        )
        listing = {
            "kind": "category",
            "category_id": category.id,
            "title": category.name,
            "all": show_all,
            "mode": "cards",
        }
        hidden = 0
        if diet is not None and offset == 0:
            hidden = self._unfiltered_total(category_id=category.id) - total
        if not total:
            s = c.session
            s.state = State.CATEGORY
            s.listing = {**listing, "offset": 0, "ids": []}
            if hidden > 0:
                return [self._diet_notice(c, "diet.hidden_all", query=category.name)]
            body = t(c.lang, "category.empty", category=category.name)
            return [OutButtons(body, (self._menu_button(c),), "category.empty")]
        carousel_cards = self._carousel_cards(c, page)
        if carousel_cards is None:  # the current one-card-per-product page
            page = page[:CARD_PAGE_SIZE]
        body_key = "category.body"
        body = t(
            c.lang,
            body_key,
            category=category.name,
            start=offset + 1,
            end=offset + len(page),
            total=total,
        )
        if carousel_cards is not None:
            replies = self._product_carousel(
                c, page, carousel_cards, total, offset, listing, body, body_key, category.name
            )
        else:
            replies = self._render_product_page(c, page, total, offset, listing, body, body_key)
        if hidden > 0:
            replies.append(self._diet_notice(c, "diet.hidden_some", count=hidden))
        return replies

    def _more_row(self, c: _Turn) -> Row:
        return Row(f"{ACT}more", t(c.lang, "list.more"), t(c.lang, "list.more_desc"))

    def _more(self, c: _Turn, *, restart_all: bool = False) -> list[Outbound]:
        """Next page of the current list; with ``restart_all``, page 1 ignoring the diet."""
        listing = c.session.listing
        if not listing:
            return self._main_menu(c)
        if restart_all and listing["kind"] == "group":  # groups are never diet-filtered
            return self._main_menu(c)
        show_all = restart_all or bool(listing.get("all"))
        if restart_all:
            offset = 0
        else:
            mode = listing.get("mode")
            step = (listing.get("shown") or CARD_PAGE_SIZE) if mode == "cards" else PAGE_SIZE
            if mode == "results":
                step = listing.get("shown", PAGE_SIZE)
            offset = listing.get("offset", 0) + step
        return self._dispatch_listing(c, listing, offset, show_all)

    def _dispatch_listing(
        self, c: _Turn, listing: dict, offset: int, show_all: bool
    ) -> list[Outbound]:
        if listing["kind"] == "group":
            group = Category(listing["group_id"], listing["title"], None, 0)
            return self._show_group(c, group, offset)
        if listing["kind"] == "category":
            category = Category(listing["category_id"], listing["title"], "", 0)
            return self._show_category(c, category, offset, show_all)
        if listing["kind"] == "search":
            return self._show_search(c, listing["query"], offset, show_all)
        return self._main_menu(c)

    def _show_current_listing(self, c: _Turn) -> list[Outbound]:
        """Re-render the page the customer is currently looking at (no offset change)."""
        listing = c.session.listing
        if not listing:
            return self._main_menu(c)
        offset, show_all = listing.get("offset", 0), bool(listing.get("all"))
        return self._dispatch_listing(c, listing, offset, show_all)

    def _continue_shopping(self, c: _Turn) -> list[Outbound]:
        """After adding a card's product to the cart: go up to the group's category
        list, never re-send the product cards already visible in the chat."""
        listing = c.session.listing or {}
        by_id = {cat.id: cat for cat in self._all_categories()}
        group_id = None
        if listing.get("kind") == "category":
            category = by_id.get(listing.get("category_id"))
            group_id = category.parent_id if category else None
        elif listing.get("kind") == "group":
            group_id = listing.get("group_id")
        group = by_id.get(group_id) if group_id else None
        if group is None:
            return self._main_menu(c)
        return self._show_group(c, group, 0)

    def _product_line(self, c: _Turn, product: ProductSummary) -> str:
        line = self._money(product.price, product.currency)
        if not product.in_stock:
            line += " · " + t(c.lang, "product.out_of_stock_tag")
        return line

    def _product_list(
        self,
        c: _Turn,
        page: list[ProductSummary],
        total: int,
        offset: int,
        listing: dict,
        body: str,
        key: str,
    ) -> list[Outbound]:
        """A ``choose which one`` list of rows (used only while a quantity is pending)."""
        rows = [
            Row(self._reply(c, Intent.PRODUCT_SELECT, p.id), p.name, self._product_line(c, p))
            for p in page
        ]
        if offset + len(page) < total:
            rows.append(self._more_row(c))
        s = c.session
        s.state = State.CATEGORY
        s.listing = {**listing, "offset": offset, "ids": [p.id for p in page]}
        return [OutList(body, t(c.lang, "list.choose"), tuple(rows), key)]

    def _card_image_url(self, product: ProductSummary) -> str | None:
        url = product.image_url
        if not url:
            return None
        if not url.startswith("https://"):
            logger.warning("engine: ignoring non-https product image_url")
            return None
        return url

    def _card_text_and_buttons(
        self, c: _Turn, product: ProductSummary
    ) -> tuple[str, tuple[Button, ...]]:
        lines = [product.name, self._money(product.price, product.currency)]
        if not product.in_stock:
            lines.append(t(c.lang, "product.out_of_stock_tag"))
        ref = self._ref(c, product.id)
        buttons = (
            Button(f"{ACT}addcard:{ref}", t(c.lang, "btn.add_to_cart")),
            Button(f"{ACT}ordernow:{ref}", t(c.lang, "btn.order_now")),
        )
        return "\n".join(lines), buttons

    def _product_card(self, c: _Turn, product: ProductSummary) -> OutProductCard:
        body, buttons = self._card_text_and_buttons(c, product)
        return OutProductCard(body, buttons, self._card_image_url(product), "product.card")

    def _carousel_cards(
        self, c: _Turn, page: list[ProductSummary]
    ) -> tuple[CarouselCard, ...] | None:
        """Carousel cards for a whole page, or ``None`` when it cannot be one carousel:
        fewer than 2 products, or any product without a usable image."""
        if not 2 <= len(page) <= MAX_CAROUSEL_CARDS:
            return None
        cards = []
        for product in page:
            url = self._card_image_url(product)
            if url is None:
                return None
            body, buttons = self._card_text_and_buttons(c, product)
            cards.append(CarouselCard(url, body, buttons))
        return tuple(cards)

    def _product_carousel(
        self,
        c: _Turn,
        page: list[ProductSummary],
        cards: tuple[CarouselCard, ...],
        total: int,
        offset: int,
        listing: dict,
        body: str,
        key: str,
        category_name: str,
    ) -> list[Outbound]:
        """One carousel for the page, then Show more (if any) and Menu. The ordinary
        intro + cards ride along as ``fallback`` should WhatsApp reject the carousel."""
        s = c.session
        s.state = State.CATEGORY
        s.listing = {**listing, "offset": offset, "shown": len(page), "ids": [p.id for p in page]}
        fallback = (OutText(body, key), *(self._product_card(c, p) for p in page))
        carousel = OutCarousel(
            t(c.lang, "carousel.body", category=category_name), cards, fallback, "carousel.body"
        )
        return [carousel, self._cards_footer(c, offset + len(page) < total)]

    def _cards_footer(self, c: _Turn, more: bool) -> OutButtons:
        buttons = []
        if more:
            buttons.append(Button(f"{ACT}more", t(c.lang, "list.more")))
        buttons.append(self._menu_button(c))
        return OutButtons(t(c.lang, "cards.footer"), tuple(buttons), "cards.footer")

    def _product_cards(
        self,
        c: _Turn,
        page: list[ProductSummary],
        total: int,
        offset: int,
        listing: dict,
        body: str,
        key: str,
    ) -> list[Outbound]:
        """One card per product (image if available, name + price, Add to cart/Order now),
        then a footer with Show more (if there is another page) and Menu."""
        s = c.session
        s.state = State.CATEGORY
        s.listing = {**listing, "offset": offset, "shown": len(page), "ids": [p.id for p in page]}
        replies: list[Outbound] = [OutText(body, key)]
        replies.extend(self._product_card(c, p) for p in page)
        replies.append(self._cards_footer(c, offset + len(page) < total))
        return replies

    def _product_results(
        self,
        c: _Turn,
        page: list[ProductSummary],
        total: int,
        offset: int,
        listing: dict,
        body: str,
        key: str,
    ) -> list[Outbound]:
        """Typed-search results as one compact list (name + price from the port). Up to
        10 rows fit; when more remain, 9 products plus a "Show more" row. Tapping a row
        sends that product's card (``ACT:showcard``)."""
        if offset + len(page) < total:
            page = page[:PAGE_SIZE]
        rows = [
            Row(f"{ACT}showcard:{self._ref(c, p.id)}", p.name, self._product_line(c, p))
            for p in page
        ]
        if offset + len(page) < total:
            rows.append(self._more_row(c))
        s = c.session
        s.state = State.CATEGORY
        s.listing = {**listing, "offset": offset, "shown": len(page), "ids": [p.id for p in page]}
        return [OutList(body, t(c.lang, "list.choose"), tuple(rows), key)]

    def _render_product_page(
        self,
        c: _Turn,
        page: list[ProductSummary],
        total: int,
        offset: int,
        listing: dict,
        body: str,
        key: str,
    ) -> list[Outbound]:
        """Rows while a quantity is pending (``ADD_TO_CART`` disambiguation); cards otherwise."""
        if listing.get("mode") == "list":
            return self._product_list(c, page, total, offset, listing, body, key)
        if listing.get("mode") == "results":
            return self._product_results(c, page, total, offset, listing, body, key)
        return self._product_cards(c, page, total, offset, listing, body, key)

    # ------------------------------------------------------------------ search & products

    def _find_products(
        self, c: _Turn, query: str, offset: int, show_all: bool = False, *, page_size: int
    ) -> _Found:
        """Search through the port with the saved diet (unless ``show_all``).

        Whole query first, then each word (longest first). If the diet hides every match,
        ``hidden`` says how many exist, so the customer is told instead of seeing "no
        results". ``resolve_product_names`` has no diet filter, so it is used only when no
        diet applies.
        """
        diet = self._diet(c, show_all)
        words = sorted({w for w in query.split() if len(w) >= 3}, key=lambda w: (-len(w), w))
        candidates = [query, *(w for w in words if w != query)]
        hidden_only: _Found | None = None
        for candidate in candidates:
            page, total = self.backend.list_products(
                search=candidate, diet=diet, limit=page_size, offset=offset
            )
            if total:
                hidden = 0
                if diet is not None and offset == 0:
                    hidden = self._unfiltered_total(search=candidate) - total
                return _Found(page, total, candidate, True, hidden)
            if diet is not None and hidden_only is None:
                unfiltered = self._unfiltered_total(search=candidate)
                if unfiltered:
                    hidden_only = _Found([], 0, candidate, True, unfiltered)
        if hidden_only is not None:
            return hidden_only
        if diet is None and offset == 0:
            found = self.backend.resolve_product_names(query, limit=page_size)
            if found:
                return _Found(found, len(found), query, False, 0)
        return _Found([], 0, query, False, 0)

    def _search(
        self, c: _Turn, query: str, *, quantity: int | None = None, adding: bool = False
    ) -> list[Outbound]:
        # A quantity to place (typed or already pending) needs an exact product, so the
        # disambiguation step picks a product for the quantity; a plain search gets a
        # compact results list whose rows open product cards.
        query = scrub_query(
            query
        )  # no phone numbers, links or handles in a search or a stored listing
        disambiguating = adding or quantity is not None
        mode = "list" if disambiguating else "results"
        page_size = PAGE_SIZE if disambiguating else MAX_ROWS
        found = self._find_products(c, query, 0, page_size=page_size)
        kind = "search" if found.pageable else "ids"
        listing = {"kind": kind, "query": found.query, "mode": mode}
        if not found.total:
            if found.hidden:
                s = c.session
                s.state = State.CATEGORY
                s.listing = {**listing, "all": False, "offset": 0, "ids": []}
                return [self._diet_notice(c, "diet.hidden_all", query=query)]
            # The query can be a processed fragment of a whole sentence (and customer words are
            # never repeated back), so the reply is the same neutral sentence either way.
            if getattr(self.pipeline, "llm_fallback_enabled", False):
                body = t(c.lang, "search.none_neutral")
                return [OutButtons(body, (self._menu_button(c),), "search.none_neutral")]
            body = t(c.lang, "search.none")
            return [OutButtons(body, (self._menu_button(c),), "search.none")]
        if found.total == 1 and disambiguating:
            replies = self._select_product(c, found.page[0].id, quantity)
        elif found.total == 1:
            # A plain search with one match: the same card as browsing (re-checked via the
            # port). Cards always add 1, so a typed quantity keeps the detail path above.
            s = c.session
            s.state = State.CATEGORY
            s.listing = {**listing, "all": False, "offset": 0, "ids": [found.page[0].id]}
            replies = self._show_card(c, found.page[0].id, relist=False)
        else:
            if disambiguating:
                c.session.pending = {"kind": "choose", "quantity": quantity}
            body = t(c.lang, "choose.product")
            replies = self._render_product_page(
                c, found.page, found.total, 0, listing, body, "choose.product"
            )
        if found.hidden:
            replies.append(self._diet_notice(c, "diet.hidden_some", count=found.hidden))
        return replies

    def _show_search(
        self, c: _Turn, query: str, offset: int, show_all: bool = False
    ) -> list[Outbound]:
        # Pagination re-entry: keep whichever mode (list/cards) the listing started in.
        mode = (c.session.listing or {}).get("mode", "cards")
        page_size = {"list": PAGE_SIZE, "results": MAX_ROWS}.get(mode, CARD_PAGE_SIZE)
        found = self._find_products(c, query, offset, show_all, page_size=page_size)
        if not found.page:
            return self._main_menu(c)
        body = t(
            c.lang,
            "search.results",
            start=offset + 1,
            end=offset + len(found.page),
            total=found.total,
        )
        listing = {"kind": "search", "query": found.query, "all": show_all, "mode": mode}
        if mode == "results" and found.total == 1:  # e.g. "Show anyway" surfaced a lone match
            c.session.state = State.CATEGORY
            c.session.listing = {**listing, "offset": 0, "ids": [found.page[0].id]}
            replies = self._show_card(c, found.page[0].id, relist=False)
            if found.hidden:
                replies.append(self._diet_notice(c, "diet.hidden_some", count=found.hidden))
            return replies
        replies = self._render_product_page(
            c, found.page, found.total, offset, listing, body, "search.results"
        )
        if found.hidden:
            replies.append(self._diet_notice(c, "diet.hidden_some", count=found.hidden))
        return replies

    # ------------------------------------------------------------------ product card actions

    @staticmethod
    def _card_default_variant(product) -> str:
        """The variant a card's quick actions add: the first in-stock one, else the first."""
        in_stock = [v for v in product.variants if v.in_stock]
        return (in_stock[0] if in_stock else product.variants[0]).id

    def _card_stale(self, c: _Turn) -> list[Outbound]:
        """A card's button pointed at a product that is now missing/inactive/out of stock."""
        return [self._text(c, "cards.stale"), *self._show_current_listing(c)]

    def _show_card(self, c: _Turn, product_id: str, *, relist: bool = True) -> list[Outbound]:
        """Re-check the product via the port, then send its card (or say it is out of
        stock). ``relist=False`` (a search that itself just found this one product) goes
        to the main menu when it is gone, since re-showing the listing would search again."""
        try:
            product = self.backend.get_product(product_id)
        except (NotFound, ProductUnavailable):
            if relist:
                return self._card_stale(c)
            return [self._text(c, "cards.stale"), *self._main_menu(c)]
        if not product.in_stock or not any(v.in_stock for v in product.variants):
            body = t(c.lang, "product.unavailable", name=product.name)
            buttons = (self._menu_button(c), self._button(c, Intent.VIEW_CART.value, "btn.cart"))
            return [OutButtons(body, buttons, "product.unavailable")]
        return [self._product_card(c, product)]

    def _card_add_to_cart(self, c: _Turn, product_id: str) -> list[Outbound]:
        try:
            product = self.backend.get_product(product_id)
            variant_id = self._card_default_variant(product)
            cart = self.backend.add_item(c.customer.id, variant_id, 1)
        except (NotFound, ProductUnavailable):
            return self._card_stale(c)
        self._cart_changed(c)
        line = next((ln for ln in cart.lines if ln.variant_id == variant_id), None)
        body = t(c.lang, "cart.added", qty=1, name=line.name if line else product.name)
        buttons = (
            self._button(c, Intent.VIEW_CART.value, "btn.cart"),
            self._button(c, f"{ACT}continue", "btn.continue_shopping"),
        )
        return [OutButtons(body, buttons, "cart.added")]

    def _card_order_now(self, c: _Turn, product_id: str) -> list[Outbound]:
        try:
            product = self.backend.get_product(product_id)
            variant_id = self._card_default_variant(product)
            self.backend.add_item(c.customer.id, variant_id, 1)
        except (NotFound, ProductUnavailable):
            return self._card_stale(c)
        self._cart_changed(c)
        return self._start_checkout(c)

    def _select_product(self, c: _Turn, product_id: str, quantity: int | None) -> list[Outbound]:
        s = c.session
        product = self.backend.get_product(product_id)
        s.product_id = product.id
        s.state = State.PRODUCT
        in_stock = [v for v in product.variants if v.in_stock]
        if not product.in_stock or not in_stock:
            body = t(c.lang, "product.unavailable", name=product.name)
            buttons = (self._menu_button(c), self._button(c, Intent.VIEW_CART.value, "btn.cart"))
            return [OutButtons(body, buttons, "product.unavailable")]

        single = len(product.variants) == 1
        lines = [product.name]
        price = product.variants[0].price if single else product.price
        currency = product.variants[0].currency if single else product.currency
        lines.append(t(c.lang, "product.price", price=self._money(price, currency)))
        if product.pack_size:
            lines.append(t(c.lang, "product.pack", pack=product.pack_size))
        if product.protein_g is not None:
            lines.append(t(c.lang, "product.protein", protein=product.protein_g))
        if product.calories_kcal is not None:
            lines.append(t(c.lang, "product.kcal", kcal=product.calories_kcal))
        if (product.protein_g is not None or product.calories_kcal is not None) and not (
            product.nutrition_verified
        ):
            lines.append(t(c.lang, "product.unverified"))
        if product.description:
            lines.append(product.description)
        detail = OutText("\n".join(lines), "product.detail")

        if single:
            variant = product.variants[0]
            if quantity is not None:
                return self._add(c, variant.id, quantity)
            return [detail, *self._ask_quantity(c, {"kind": "add", "variant_id": variant.id})]
        rows = tuple(
            Row(self._reply(c, Intent.ADD_TO_CART, v.id, quantity), v.label,
                self._money(v.price, v.currency))
            for v in in_stock[:MAX_ROWS]
        )  # fmt: skip
        body = t(c.lang, "product.choose_variant", name=product.name)
        return [detail, OutList(body, t(c.lang, "list.choose"), rows, "product.choose_variant")]

    def _ask_quantity(self, c: _Turn, pending: dict) -> list[Outbound]:
        s = c.session
        s.pending = pending
        s.state = State.QUANTITY
        variant_id = pending["variant_id"]
        buttons = tuple(
            Button(self._reply(c, Intent.ADD_TO_CART, variant_id, n), str(n))
            for n in QUICK_QUANTITIES
        )
        return [OutButtons(t(c.lang, "product.ask_qty"), buttons, "product.ask_qty")]

    # ------------------------------------------------------------------ AI Planner handoff

    def _resolve_pick(self, name: str) -> tuple[ProductSummary, str | None] | None:
        """Resolve one plan pick to a product and a default variant id via the port.

        ``None`` means unresolved (no catalog match). A resolved product with no
        in-stock variant returns the product with ``variant_id=None`` (out of stock).
        The customer is never asked to choose a variant here, so the first in-stock one
        is used.
        """
        matches = self.backend.resolve_product_names(name, limit=1)
        if not matches:
            return None
        summary = matches[0]
        detail = self.backend.get_product(summary.id)
        in_stock = [v for v in detail.variants if v.in_stock]
        if not detail.in_stock or not in_stock:
            return summary, None
        return summary, in_stock[0].id

    def _plan_summary_line(self, c: _Turn, e: Entities) -> str:
        """Restate only the fields the customer sent; never compute or add anything."""
        parts = []
        if e.goal == "healthy_eating":
            parts.append(t(c.lang, "plan.clause_goal_healthy_eating"))
        nutrients = []
        if e.calories is not None:
            nutrients.append(t(c.lang, "plan.clause_calories", kcal=e.calories))
        if e.protein_g is not None:
            nutrients.append(t(c.lang, "plan.clause_protein", protein=e.protein_g))
        if nutrients:
            parts.append(t(c.lang, "plan.and").join(nutrients) + t(c.lang, "plan.per_day_suffix"))
        if not parts:
            return t(c.lang, "plan.no_valid_fields")
        return t(c.lang, "plan.summary", details=", ".join(parts))

    def _plan_received(self, c: _Turn, e: Entities) -> list[Outbound]:
        resolved: list[tuple[ProductSummary, str]] = []
        unresolved: list[str] = []
        out_of_stock: list[str] = []
        for name in e.picks:
            found = self._resolve_pick(name)
            if found is None:
                unresolved.append(name)
                continue
            summary, variant_id = found
            if variant_id is None:
                out_of_stock.append(summary.name)
                continue
            resolved.append((summary, variant_id))

        if e.diet:
            c.customer = self.backend.update_profile(c.customer.id, diet=e.diet)

        lines = [self._plan_summary_line(c, e)]
        for summary, _ in resolved:
            lines.append(
                t(c.lang, "plan.pick_line", name=summary.name,
                  price=self._money(summary.price, summary.currency))
            )  # fmt: skip
        if unresolved:
            lines.append(t(c.lang, "plan.picks_unresolved", names="; ".join(unresolved)))
        if out_of_stock:
            lines.append(t(c.lang, "plan.picks_out_of_stock", names="; ".join(out_of_stock)))

        c.session.plan_picks = [variant_id for _, variant_id in resolved]
        buttons = [self._browse_menu_button(c)]
        if resolved:
            buttons.insert(0, self._button(c, f"{ACT}planadd", "btn.add_all_to_cart"))
        return [OutButtons("\n".join(lines), tuple(buttons), "plan.summary")]

    def _add_all_plan_picks(self, c: _Turn) -> list[Outbound]:
        variant_ids, c.session.plan_picks = c.session.plan_picks, []
        if not variant_ids:
            return self._main_menu(c)
        skipped = 0
        cart = None
        for variant_id in variant_ids:
            try:
                cart = self.backend.add_item(c.customer.id, variant_id, 1)
            except (ProductUnavailable, NotFound):
                skipped += 1
        self._cart_changed(c)
        if cart is None:
            cart = self.backend.get_cart(c.customer.id)
        replies = [self._text(c, "plan.added_to_cart")]
        if skipped:
            replies.append(self._text(c, "plan.picks_skipped_at_add", count=skipped))
        return [*replies, *self._cart_messages(c, cart)]

    # ------------------------------------------------------------------ LLM cart requests

    def _llm_cart_confirmation(self, c: _Turn, r: IntentResult) -> list[Outbound] | None:
        """Ask before changing the cart for a request the LLM understood. Nothing changes
        until the customer taps Yes. ``None`` = no usable line (the caller falls back to the
        read-only/rules handling). Every slug is re-checked through the port here."""
        intent = r.intent
        lines: dict[str, dict] = {}
        cart = self.backend.get_cart(c.customer.id) if intent is not Intent.ADD_TO_CART else None
        for item in r.llm_items:
            if not item.product_slug:
                continue
            if intent is Intent.ADD_TO_CART:
                try:
                    product = self.backend.get_product(item.product_slug)
                except (NotFound, ProductUnavailable):
                    continue
                if not product.in_stock or not any(v.in_stock for v in product.variants):
                    continue
                line = lines.setdefault(
                    product.id,
                    {
                        "variant_id": self._card_default_variant(product),
                        "name": product.name,
                        "qty": 0,
                    },
                )
                line["qty"] += item.quantity or 1
            else:
                cart_line = next(
                    (ln for ln in cart.lines if ln.product_id == item.product_slug), None
                )
                if cart_line is None or (intent is Intent.UPDATE_QUANTITY and not item.quantity):
                    continue
                lines[cart_line.line_id] = {
                    "line_id": cart_line.line_id,
                    "name": cart_line.name,
                    "qty": item.quantity or cart_line.quantity,
                }
        if not lines:
            return None
        action = {Intent.ADD_TO_CART: "add", Intent.REMOVE_FROM_CART: "remove"}.get(
            intent, "update"
        )
        picked = [{**ln, "qty": min(ln["qty"], PORT_MAX_QUANTITY)} for ln in lines.values()]
        c.session.pending = {"kind": "llm_confirm", "action": action, "lines": picked}
        llm_state.stat("cart_confirm.offered")
        if action == "update":
            shown = ", ".join(f"{ln['name']} → {ln['qty']}" for ln in picked)
        elif action == "remove":
            shown = ", ".join(ln["name"] for ln in picked)
        else:
            shown = ", ".join(f"{ln['qty']} × {ln['name']}" for ln in picked)
        key = f"llm.confirm_{action}"
        buttons = (
            self._button(c, f"{ACT}llmyes", "btn.yes"),
            self._button(c, f"{ACT}llmno", "btn.no"),
        )
        return [OutButtons(t(c.lang, key, items=shown), buttons, key)]

    def _fuzzy_cart_confirmation(self, c: _Turn, r: IntentResult) -> list[Outbound] | None:
        """A cart change guessed by the fuzzy stage (typo correction) is never applied on
        its own: it goes through the same Yes/No question as an LLM-derived change. Only the
        cases that would change the cart straight away are intercepted (one clear product or
        line and a quantity); anything that already asks the customer something stays as is."""
        e = r.entities
        if not e.product_query or e.reference_id:
            return None
        if r.intent is Intent.ADD_TO_CART:
            if e.quantity is None:
                return None  # the existing flow asks how many first: no cart change
            matches = self.backend.resolve_product_names(e.product_query, limit=3)
            exact = [m for m in matches if _fold(m.name) == _fold(e.product_query)]
            matches = exact if len(exact) == 1 else matches
            if len(matches) != 1:
                return None  # several matches: the customer picks one by tapping
            items = (LLMItem(matches[0].id, e.product_query, e.quantity),)
        else:
            lines = [
                ln
                for ln in self.backend.get_cart(c.customer.id).lines
                if _words_match(e.product_query, ln.name)
            ]
            if len(lines) != 1:
                return None
            quantity = e.quantity if r.intent is Intent.UPDATE_QUANTITY else None
            if r.intent is Intent.UPDATE_QUANTITY and quantity is None:
                return None
            items = (LLMItem(lines[0].product_id, e.product_query, quantity),)
        return self._llm_cart_confirmation(c, replace(r, llm_items=items))

    def _llm_apply(self, c: _Turn) -> list[Outbound]:
        pending, c.session.pending = c.session.pending, None
        if not pending or pending.get("kind") != "llm_confirm":
            return self._stale_answer(c)  # a stale tap: nothing is waiting
        llm_state.stat("cart_confirm.accepted")
        action, cid, skipped = pending["action"], c.customer.id, 0
        cart = None
        for line in pending["lines"]:
            try:
                if action == "add":
                    cart = self.backend.add_item(cid, line["variant_id"], line["qty"])
                elif action == "remove":
                    cart = self.backend.remove_item(cid, line["line_id"])
                else:
                    cart = self.backend.update_quantity(cid, line["line_id"], line["qty"])
            except (ProductUnavailable, NotFound):
                skipped += 1
        self._cart_changed(c)
        if cart is None:
            cart = self.backend.get_cart(cid)
        replies = [self._text(c, "llm.done")]
        if skipped:
            replies.append(self._text(c, "plan.picks_skipped_at_add", count=skipped))
        return [*replies, *self._cart_messages(c, cart)]

    # ------------------------------------------------------------------ native catalog order

    def _catalog_order(self, c: _Turn, order: dict) -> list[Outbound]:
        """A native WhatsApp cart order. Product IDs and quantities are all that is taken
        from the message; names, stock and prices come from the port. The order is the
        customer's authoritative selection, so it REPLACES the IVA cart."""
        if not c.customer.onboarding_complete:
            return [self._text(c, "order.setup_first"), *self._onboarding(c, Inbound())]
        expected = getattr(settings, "IVA_CATALOG_ID", "") or ""
        if expected and order.get("catalog_id") != expected:
            logger.info("engine: catalog order ignored (other catalog)")
            return self._order_menu_reply(c, "order.wrong_catalog")
        items = order.get("items") or []
        if not items:
            logger.info("engine: catalog order unreadable unreadable=%s", order.get("unreadable"))
            return self._order_menu_reply(c, "order.unreadable")
        cap = effective_cap(getattr(settings, "IVA_CATALOG_ORDER_MAX_QTY", 10))
        result = validate_order(self.backend, items, cap)
        result.unreadable += int(order.get("unreadable") or 0)
        if not result.lines:
            self._log_order(result, [])
            return [*self._order_notes(c, result), *self._order_menu_reply(c, "order.none_added")]
        added = self._replace_cart(c, result)
        self._log_order(result, [ln.product_id for ln in added])
        self._cart_changed(c)
        cart = self.backend.get_cart(c.customer.id)
        return [
            self._text(c, "order.cart_replaced"),
            *self._order_notes(c, result),
            *self._cart_messages(c, cart),
        ]

    def _order_menu_reply(self, c: _Turn, key: str) -> list[Outbound]:
        return [OutButtons(t(c.lang, key), (self._browse_menu_button(c),), key)]

    def _replace_cart(self, c: _Turn, result: ValidatedOrder) -> list:
        """Make the cart hold exactly the validated lines. Lines already in the cart are
        updated in place, new ones added, the rest removed, so a failure part-way never
        leaves the customer with an emptied cart."""
        cid = c.customer.id
        existing = {ln.variant_id: ln for ln in self.backend.get_cart(cid).lines}
        added = []
        for line in result.lines:
            try:
                current = existing.get(line.variant_id)
                if current is not None:
                    self.backend.update_quantity(cid, current.line_id, line.quantity)
                else:
                    self.backend.add_item(cid, line.variant_id, line.quantity)
            except (ProductUnavailable, NotFound, InvalidQuantity):
                result.unavailable.append(line.name)  # went away between check and add
                continue
            added.append(line)
        keep = {ln.variant_id for ln in added}
        for variant_id, current in existing.items():
            if added and variant_id not in keep:
                self.backend.remove_item(cid, current.line_id)
        return added

    def _order_notes(self, c: _Turn, r: ValidatedOrder) -> list[Outbound]:
        notes: list[Outbound] = []
        if r.unknown:
            notes.append(self._text(c, "order.excluded_unknown", count=len(r.unknown)))
        if r.unavailable:
            names = "; ".join(r.unavailable)
            notes.append(self._text(c, "order.excluded_unavailable", names=names))
        if r.bad_quantity:
            notes.append(self._text(c, "order.excluded_quantity", count=len(r.bad_quantity)))
        if r.unreadable:
            notes.append(self._text(c, "order.excluded_unreadable", count=r.unreadable))
        if r.clamped:
            notes.append(self._text(c, "order.clamped", max=r.cap, names="; ".join(r.clamped)))
        if r.price_changed:
            notes.append(self._text(c, "order.price_changed", names="; ".join(r.price_changed)))
        return notes

    @staticmethod
    def _log_order(r: ValidatedOrder, accepted: list[str]) -> None:
        logger.info(
            "engine: catalog order accepted=%d %s unknown=%d %s unavailable=%d "
            "bad_quantity=%d unreadable=%d clamped=%d price_changed=%d",
            len(accepted), accepted, len(r.unknown), r.unknown, len(r.unavailable),
            len(r.bad_quantity), r.unreadable, len(r.clamped), len(r.price_changed),
        )  # fmt: skip

    # ------------------------------------------------------------------ cart

    def _cart_messages(self, c: _Turn, cart: CartView) -> list[Outbound]:
        c.session.state = State.CART
        if not cart.lines:
            return [OutButtons(t(c.lang, "cart.empty"), (self._menu_button(c),), "cart.empty")]
        lines = [t(c.lang, "cart.title")]
        for line in cart.lines:
            total = self._money(line.line_total, cart.currency)
            lines.append(t(c.lang, "cart.line", name=line.name, qty=line.quantity, total=total))
        subtotal = t(c.lang, "cart.subtotal", subtotal=self._money(cart.subtotal, cart.currency))
        buttons = (
            self._button(c, Intent.CHECKOUT.value, "btn.checkout"),
            self._button(c, f"{ACT}edit", "btn.edit_cart"),
            self._menu_button(c),
        )
        return [
            OutText("\n".join(lines), "cart.title"),
            OutButtons(subtotal, buttons, "cart.subtotal"),
        ]

    def _show_cart(self, c: _Turn) -> list[Outbound]:
        return self._cart_messages(c, self.backend.get_cart(c.customer.id))

    def _cart_changed(self, c: _Turn) -> None:
        c.session.idempotency_key = None  # a changed cart is a new checkout attempt
        c.session.pending = None

    def _add(self, c: _Turn, variant_id: str, quantity: int) -> list[Outbound]:
        cart = self.backend.add_item(c.customer.id, variant_id, quantity)
        self._cart_changed(c)
        line = next((ln for ln in cart.lines if ln.variant_id == variant_id), None)
        added = self._text(c, "cart.added", qty=quantity, name=line.name if line else "")
        return [added, *self._cart_messages(c, cart)]

    def _find_line(self, cart: CartView, line_id: str):
        return next((ln for ln in cart.lines if ln.line_id == line_id), None)

    def _remove_line(self, c: _Turn, line_id: str) -> list[Outbound]:
        line = self._find_line(self.backend.get_cart(c.customer.id), line_id)
        cart = self.backend.remove_item(c.customer.id, line_id)
        self._cart_changed(c)
        removed = self._text(c, "cart.removed", name=line.name if line else "")
        return [removed, *self._cart_messages(c, cart)]

    def _update_line(self, c: _Turn, line_id: str, quantity: int) -> list[Outbound]:
        cart = self.backend.update_quantity(c.customer.id, line_id, quantity)
        self._cart_changed(c)
        line = self._find_line(cart, line_id)
        updated = self._text(c, "cart.updated", name=line.name if line else "", qty=quantity)
        return [updated, *self._cart_messages(c, cart)]

    def _ask_line_quantity(self, c: _Turn, line_id: str) -> list[Outbound]:
        line = self._find_line(self.backend.get_cart(c.customer.id), line_id)
        if line is None:
            raise NotFound("Cart line not found.")
        c.session.pending = {"kind": "update", "line_id": line_id}
        c.session.state = State.QUANTITY
        return [self._text(c, "qty.ask_update", name=line.name)]

    def _edit_cart(self, c: _Turn) -> list[Outbound]:
        cart = self.backend.get_cart(c.customer.id)
        if not cart.lines:
            return self._cart_messages(c, cart)
        rows = tuple(
            Row(f"{ACT}line:{self._ref(c, ln.line_id)}", ln.name,
                f"× {ln.quantity} = {self._money(ln.line_total, cart.currency)}")
            for ln in cart.lines[:MAX_ROWS]
        )  # fmt: skip
        c.session.state = State.CART
        return [OutList(t(c.lang, "cart.edit"), t(c.lang, "list.choose"), rows, "cart.edit")]

    def _line_actions(self, c: _Turn, line_id: str) -> list[Outbound]:
        line = self._find_line(self.backend.get_cart(c.customer.id), line_id)
        if line is None:
            raise NotFound("Cart line not found.")
        buttons = (
            Button(self._reply(c, Intent.UPDATE_QUANTITY, line_id), t(c.lang, "btn.change_qty")),
            Button(self._reply(c, Intent.REMOVE_FROM_CART, line_id), t(c.lang, "btn.remove")),
            self._button(c, Intent.VIEW_CART.value, "btn.cart"),
        )
        body = t(c.lang, "cart.line_actions", name=line.name, qty=line.quantity)
        return [OutButtons(body, buttons, "cart.line_actions")]

    def _pick_line(
        self,
        c: _Turn,
        action: str,
        query: str | None,
        choice_index: int | None,
        quantity: int | None,
    ) -> list[Outbound]:
        cart = self.backend.get_cart(c.customer.id)
        lines = list(cart.lines)
        if not lines:
            return self._cart_messages(c, cart)
        if choice_index:
            candidates = [lines[choice_index - 1]] if choice_index <= len(lines) else []
        elif query:
            candidates = [ln for ln in lines if _words_match(query, ln.name)]
        else:
            candidates = lines
        if not candidates:
            missing = self._text(c, "cart.not_found")
            return [missing, *self._cart_messages(c, cart)]
        specific = bool(query or choice_index) or (action == "update" and quantity is not None)
        if len(candidates) == 1 and specific:
            line = candidates[0]
            if action == "remove":
                return self._remove_line(c, line.line_id)
            if quantity is not None:
                return self._update_line(c, line.line_id, quantity)
            return self._ask_line_quantity(c, line.line_id)
        intent = Intent.REMOVE_FROM_CART if action == "remove" else Intent.UPDATE_QUANTITY
        qty = quantity if action == "update" else None
        rows = tuple(
            Row(self._reply(c, intent, ln.line_id, qty), ln.name, f"× {ln.quantity}")
            for ln in candidates[:MAX_ROWS]
        )
        body = t(c.lang, "cart.choose_line")
        return [OutList(body, t(c.lang, "list.choose"), rows, "cart.choose_line")]

    # ------------------------------------------------------------------ checkout

    def _start_checkout(self, c: _Turn) -> list[Outbound]:
        cart = self.backend.get_cart(c.customer.id)
        if not cart.lines:
            return self._cart_messages(c, cart)
        addresses = self.backend.list_addresses(c.customer.id)
        c.session.state = State.CHECKOUT_ADDRESS
        if not addresses:
            body = t(c.lang, "checkout.type_address")
            return [OutButtons(body, (self._menu_button(c),), "checkout.type_address")]
        rows = [
            Row(f"{ACT}addr:{self._ref(c, a.id)}", a.label or a.text, a.text)
            for a in addresses[: MAX_ROWS - 1]
        ]
        rows.append(Row(f"{ACT}newaddr", t(c.lang, "checkout.new_address")))
        body = t(c.lang, "checkout.ask_address")
        return [OutList(body, t(c.lang, "list.choose"), tuple(rows), "checkout.ask_address")]

    def _checkout_command(self, c: _Turn, folded: str) -> list[Outbound] | None:
        """At the address step: a message that is EXACTLY a greeting or a global command
        (any language, romanized or native) is that, not an address. Anything else,
        including an address that merely contains such a word, returns ``None``."""
        if folded in GREETING_WORDS:
            name = c.customer.name or ""
            body = t(c.lang, "checkout.welcome_back", name=name).replace(", !", "!").lstrip(", ")
            buttons = (
                Button(f"{ACT}resume", t(c.lang, "btn.continue_checkout")),
                self._menu_button(c),
            )
            return [OutButtons(body, buttons, "checkout.welcome_back")]
        if folded in CART_WORDS:
            return self._show_cart(c)
        if folded in ORDER_STATUS_WORDS:
            return self._order_status(c, None)
        if folded in HELP_WORDS:
            buttons = (self._menu_button(c), self._button(c, Intent.VIEW_CART.value, "btn.cart"))
            return [OutButtons(t(c.lang, "help"), buttons, "help")]
        if folded in LANGUAGE_WORDS:
            c.session.state = State.LANGUAGE
            return [self._language_prompt(c)]
        return None

    def _address_typed(self, c: _Turn, text: str) -> list[Outbound]:
        address_text = " ".join(text.split())
        if not ADDRESS_MIN <= len(address_text) <= ADDRESS_MAX:
            body = t(c.lang, "checkout.address_invalid")
            return [OutButtons(body, (self._menu_button(c),), "checkout.address_invalid")]
        if not looks_like_address(address_text):  # long enough, but not an address ("zork florp")
            body = t(c.lang, "checkout.address_unclear")
            return [OutButtons(body, (self._menu_button(c),), "checkout.address_unclear")]
        address = self.backend.add_address(c.customer.id, address_text)
        c.session.address_id = address.id
        return self._confirm(c)

    def _confirm(self, c: _Turn) -> list[Outbound]:
        s = c.session
        cart = self.backend.get_cart(c.customer.id)
        if not cart.lines:
            return self._cart_messages(c, cart)
        addresses = self.backend.list_addresses(c.customer.id)
        address = next((a for a in addresses if a.id == s.address_id), None)
        if address is None:
            s.address_id = None
            return self._start_checkout(c)
        if not s.idempotency_key:
            # Generated once per checkout attempt and reused on every retry.
            s.idempotency_key = uuid.uuid4().hex
        s.state = State.CHECKOUT_CONFIRM
        summary = self._cart_messages(c, cart)[0].body
        s.state = State.CHECKOUT_CONFIRM
        subtotal = t(c.lang, "cart.subtotal", subtotal=self._money(cart.subtotal, cart.currency))
        deliver = t(c.lang, "checkout.deliver_to", address=address.text)
        buttons = (
            self._button(c, f"{ACT}confirm", "btn.confirm"),
            self._button(c, f"{ACT}edit", "btn.edit_cart"),
            self._button(c, f"{ACT}cancel", "btn.cancel"),
        )
        return [
            OutText("\n".join((summary, subtotal, deliver)), "checkout.summary"),
            OutButtons(t(c.lang, "checkout.confirm"), buttons, "checkout.confirm"),
        ]

    def _place_order(self, c: _Turn) -> list[Outbound]:
        s = c.session
        if not (s.address_id and s.idempotency_key):
            return self._start_checkout(c)
        try:
            result = self.backend.checkout(c.customer.id, s.address_id, s.idempotency_key)
        except CheckoutRejected as exc:
            s.idempotency_key = None  # nothing was created; a new attempt gets a new key
            return self._rejected(c, exc.reason)
        s.order_number = result.order_number
        s.payment_url = result.payment_url
        s.state = State.AWAITING_PAYMENT
        lines = [
            t(
                c.lang,
                "checkout.created",
                order=result.order_number,
                total=self._money(result.total, result.currency),
            ),
            t(c.lang, "checkout.thanks"),
            t(
                c.lang,
                "checkout.payment_line",
                status=self._status_name(c, "payment", result.payment_status),
            ),
        ]
        if result.payment_url:
            lines.append(t(c.lang, "checkout.pay_link", url=result.payment_url))
        else:
            lines.append(t(c.lang, "checkout.no_link"))
        buttons = (
            Button(
                self._reply(c, Intent.PAYMENT_STATUS, result.order_number),
                t(c.lang, "btn.payment_status"),
            ),
            Button(
                self._reply(c, Intent.ORDER_STATUS, result.order_number),
                t(c.lang, "btn.order_status"),
            ),
            self._menu_button(c),
        )
        return [OutButtons("\n".join(lines), buttons, "checkout.created")]

    def _rejected(self, c: _Turn, reason: str) -> list[Outbound]:
        cart = self.backend.get_cart(c.customer.id)
        if reason == CheckoutRejected.ADDRESS_NOT_SERVICEABLE:
            c.session.address_id = None
            return [self._text(c, "checkout.not_serviceable"), *self._start_checkout(c)]
        key = {
            CheckoutRejected.PRICE_CHANGED: "checkout.price_changed",
            CheckoutRejected.PRODUCT_UNAVAILABLE: "checkout.product_unavailable",
            CheckoutRejected.BELOW_MINIMUM_ORDER: "checkout.below_minimum",
        }.get(reason, "checkout.rejected")
        return [self._text(c, key), *self._cart_messages(c, cart)]

    # ------------------------------------------------------------------ orders & payment

    def _get_order(self, c: _Turn, order_number: str) -> OrderStatusView | None:
        try:
            return self.backend.get_order(c.customer.id, order_number)
        except NotAuthorized:
            return None  # never reveal that another customer's order exists

    def _find_order(self, c: _Turn, order_number: str | None):
        if order_number:
            return self._get_order(c, order_number), order_number
        if c.session.order_number:
            view = self._get_order(c, c.session.order_number)
            if view is not None:
                return view, view.order_number
        return self.backend.get_latest_order(c.customer.id), None

    def _no_order(self, c: _Turn, asked: str | None) -> list[Outbound]:
        if asked:
            body, key = t(c.lang, "order.not_found", order=asked), "order.not_found"
        else:
            body, key = t(c.lang, "order.none"), "order.none"
        return [OutButtons(body, (self._menu_button(c),), key)]

    @staticmethod
    def _format_time(value) -> str:
        return timezone.localtime(value).strftime("%d-%m-%Y %H:%M")

    def _order_status(self, c: _Turn, order_number: str | None) -> list[Outbound]:
        view, asked = self._find_order(c, order_number)
        if view is None:
            return self._no_order(c, asked)
        lines = [
            t(
                c.lang,
                "order.status",
                order=view.order_number,
                status=self._status_name(c, "order", view.order_status),
            ),
            t(c.lang, "order.payment", status=self._status_name(c, "payment", view.payment_status)),
        ]
        times = [self._format_time(v) for v in (view.eta_start, view.eta_end) if v is not None]
        if times:
            lines.append(t(c.lang, "order.eta", eta=" – ".join(dict.fromkeys(times))))
        else:
            lines.append(t(c.lang, "order.eta_na"))
        if view.tracking_id:
            lines.append(t(c.lang, "order.tracking", tracking=view.tracking_id))
        else:
            lines.append(t(c.lang, "order.tracking_na"))
        buttons = (self._menu_button(c),)
        if view.payment_status != PaymentStatus.PAID:  # a paid order has no payment to check
            buttons = (
                Button(
                    self._reply(c, Intent.PAYMENT_STATUS, view.order_number),
                    t(c.lang, "btn.payment_status"),
                ),
                *buttons,
            )
        return [OutButtons("\n".join(lines), buttons, "order.status")]

    def _payment_status(self, c: _Turn, order_number: str | None) -> list[Outbound]:
        s = c.session
        view, asked = self._find_order(c, order_number)
        if view is None:
            return self._no_order(c, asked)
        status = view.payment_status
        if status in _KNOWN_PAYMENT:
            key = f"payment.{status}"
            lines = [t(c.lang, key, order=view.order_number)]
        else:
            key = "payment.unknown"
            lines = [t(c.lang, key, order=view.order_number, status=status)]
        ours = s.order_number == view.order_number
        if status in (PaymentStatus.PENDING, PaymentStatus.FAILED) and ours and s.payment_url:
            lines.append(t(c.lang, "checkout.pay_link", url=s.payment_url))
        if status == PaymentStatus.PAID and ours and s.state is State.AWAITING_PAYMENT:
            s.state = State.MAIN_MENU
        buttons = (
            Button(
                self._reply(c, Intent.ORDER_STATUS, view.order_number),
                t(c.lang, "btn.order_status"),
            ),
            self._menu_button(c),
        )
        return [OutButtons("\n".join(lines), buttons, key)]


# --------------------------------------------------------------------------- shared engine

_engine_lock = threading.Lock()
_engine: ConversationEngine | None = None


def get_engine() -> ConversationEngine:
    """Shared engine built from settings (Backend Port adapter, session store, LLM)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = ConversationEngine()
        return _engine


def reset_engine() -> None:
    global _engine
    with _engine_lock:
        _engine = None
