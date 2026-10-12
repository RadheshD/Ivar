"""Stated constraints (price, diet, high protein) are never dropped by a name search, and a
price number is never a quantity. A scripted fake stands in for Groq: no network."""

from __future__ import annotations

import logging

import pytest

from apps.ai.classifier import ClassifierTimeout
from apps.ai.pipeline import IntentPipeline
from apps.conversations.engine import ConversationEngine
from apps.conversations.messages import t

from .harness import Chat, onboard
from .test_engine_recommend import (  # noqa: F401  (local_cache is an autouse fixture)
    FakeGroq,
    cart_of,
    ext,
    local_cache,
    make_chat,
    picks,
)

PHONE = "Something high protein under 300"
ABOVE_300 = {"Plant Protein Mix", "Active Protein", "High-Protein Paneer Bowl"}
PRICE_TEXTS = [
    "high protein under 15",
    "snacks under 20",
    "veg under 10",
    "below Rs 15",
    "15 rupees",
    PHONE,
]
BAD_QUANTITY = "qty.invalid"


@pytest.fixture
def groq():
    return FakeGroq()


@pytest.fixture
def chat(backend, store, clock, groq):
    return make_chat(backend, store, clock, groq)


@pytest.fixture
def plain(backend, store, clock):
    """Both LLM flags off: no fallback at all."""
    return Chat(ConversationEngine(backend, store, IntentPipeline(), clock=clock))


def prices(chat, names):
    backend = chat.engine.backend
    found = {}
    offset = 0
    while True:
        page, total = backend.list_products(limit=50, offset=offset)
        found.update({p.name: p for p in page})
        offset += len(page)
        if not page or offset >= total:
            return [found[n] for n in names]


def tap_every_choice(chat, text):
    """Send ``text`` and tap each product choice it offers; no quantity may be carried."""
    chat.send(text)
    ids = [
        i for i in chat.ids if i.startswith(("PRODUCT_SELECT:", "ADD_TO_CART:", "ACT:showcard:"))
    ]
    for reply_id in ids[:6]:
        chat.send(text)
        chat.tap(reply_id)
        assert BAD_QUANTITY not in chat.keys, (text, reply_id, chat.keys)
        assert "cart.added" not in chat.keys, (text, reply_id, chat.keys)
        assert cart_of(chat) == {}, (text, reply_id)
    return ids


# ---------------------------------------------------------------- the phone message


def test_the_phone_message_goes_to_the_recommendation_flow(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext(sort="protein_desc", max_price=300))
    chat.send(PHONE)
    assert chat.keys[0] == "recommend.intro" and groq.calls == 1
    shown = picks(chat)
    assert shown and not ABOVE_300 & set(shown)
    items = prices(chat, shown)
    assert all(p.price <= 300 for p in items)
    proteins = [p.protein_g for p in items]
    assert proteins == sorted(proteins, reverse=True)
    assert "choose.product" not in chat.keys and BAD_QUANTITY not in chat.keys
    assert t("en", "recommend.max_price", max=300) in chat.message("recommend.intro").body
    assert cart_of(chat) == {}


def test_the_price_is_read_in_code_even_when_the_model_returns_none(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext())  # the model found no price and no sort
    chat.send(PHONE)
    shown = picks(chat)
    assert shown and not ABOVE_300 & set(shown)
    assert all(p.price <= 300 for p in prices(chat, shown))
    assert "Sorted by protein" in chat.message("recommend.intro").body


def test_a_wrong_price_from_the_model_loses_to_the_price_in_the_message(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext(max_price=900))
    chat.send(PHONE)
    assert not ABOVE_300 & set(picks(chat))


def test_with_the_model_down_the_limit_still_holds_and_no_list_is_shown(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ClassifierTimeout())
    chat.send(PHONE)
    assert chat.keys[0] == "recommend.intro"
    assert "choose.product" not in chat.keys and not ABOVE_300 & set(picks(chat))
    assert all(p.price <= 300 for p in prices(chat, picks(chat)))


def test_the_limit_is_inclusive_and_nothing_above_it_appears(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext())
    chat.send("high protein under 229")
    shown = picks(chat)
    assert "Protein Smoothie" in shown
    assert all(p.price <= 229 for p in prices(chat, shown))


def test_nothing_qualifying_uses_the_existing_no_match_reply(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext())
    chat.send("high protein under 15")
    assert chat.keys == ["recommend.none"] and cart_of(chat) == {}


# ---------------------------------------------------------------- the price is never a quantity


@pytest.mark.parametrize("text", PRICE_TEXTS)
def test_no_quantity_from_a_price_flags_on(chat, groq, text):
    onboard(chat)
    for _ in range(8):
        groq.extract_queue.append(ext())
    tap_every_choice(chat, text)
    assert cart_of(chat) == {}


@pytest.mark.parametrize("text", PRICE_TEXTS)
def test_no_quantity_from_a_price_flags_off(plain, text):
    onboard(plain)
    tap_every_choice(plain, text)
    plain.send(text)
    assert BAD_QUANTITY not in plain.keys and "qty.ask" not in plain.keys
    assert cart_of(plain) == {}


def row_for(chat, slug):
    refs = chat.session().refs
    return next(
        i for i in chat.ids if i.startswith("PRODUCT_SELECT:")
        and refs.get(i.split(":")[1], i.split(":")[1]) == slug
    )  # fmt: skip


def test_the_confirmed_bug_after_the_list_the_tap_asks_how_many(plain):
    onboard(plain)
    plain.send("add protein under 300")  # a choose-one list for an add, no quantity given
    assert "choose.product" in plain.keys and cart_of(plain) == {}
    plain.tap(row_for(plain, "active-protein"))
    assert BAD_QUANTITY not in plain.keys and cart_of(plain) == {}
    assert "product.ask_qty" in plain.keys  # the existing rule for a choice with no quantity


def test_the_phone_message_with_the_flags_off_is_a_plain_search_without_a_quantity(plain):
    onboard(plain)
    plain.send(PHONE)
    assert "recommend.intro" not in plain.keys and cart_of(plain) == {}
    assert not any(i.startswith(("PRODUCT_SELECT:", "ADD_TO_CART:")) for i in plain.ids)


def test_a_bare_price_while_a_quantity_is_asked_is_not_the_quantity(plain):
    onboard(plain)
    plain.send("add makhana crunch")
    assert "product.ask_qty" in plain.keys
    plain.send("under 300")
    assert cart_of(plain) == {} and BAD_QUANTITY not in plain.keys


# ---------------------------------------------------------------- the exact-name exception


def test_a_named_product_with_a_price_still_finds_the_product(chat, groq):
    onboard(chat)
    chat.send("protein smoothie under 300")
    assert groq.calls == 0 and "recommend.intro" not in chat.keys
    assert any("Protein Smoothie" in c.body for c in chat.cards), chat.keys


def test_a_named_product_still_finds_the_product_with_the_flags_off(plain):
    onboard(plain)
    plain.send("protein smoothie under 300")
    assert BAD_QUANTITY not in plain.keys and "recommend.intro" not in plain.keys


# ---------------------------------------------------------------- other languages and orders


@pytest.mark.parametrize(
    "text",
    [
        "under 300 veg snacks",
        "300 se kam veg snacks",
        "300 లోపు వెజ్ స్నాక్స్",
        "300 lopu veg snacks",
    ],
)
def test_price_and_diet_are_applied_in_every_language(chat, groq, text):
    onboard(chat)
    groq.extract_queue.append(ext())
    chat.send(text)
    assert chat.keys[0] == "recommend.intro" and "choose.product" not in chat.keys
    items = prices(chat, picks(chat))
    assert items and all(p.price <= 300 and p.diet_type == "veg" for p in items)


def test_a_category_in_the_message_filters_without_the_model(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ClassifierTimeout())
    chat.send("under 400 protein bowls")
    shown = picks(chat)
    assert shown and all(p.category_id == "protein-bowls" for p in prices(chat, shown))
    assert all(p.price <= 400 for p in prices(chat, shown))


@pytest.mark.parametrize("flags", ["on", "off"])
def test_ordinary_adds_are_unchanged(backend, store, clock, groq, flags):
    chat = (
        make_chat(backend, store, clock, groq)
        if flags == "on"
        else Chat(ConversationEngine(backend, store, IntentPipeline(), clock=clock))
    )
    onboard(chat)
    chat.send("add 2 quinoa")  # several products match: pick one, the 2 is kept
    assert "choose.product" in chat.keys and cart_of(chat) == {}
    chat.tap(row_for(chat, "quinoa"))
    assert cart_of(chat) == {"quinoa": 2} and "cart.added" in chat.keys
    assert groq.calls == 0
    chat.send("ad 2 makhana crunch")
    assert cart_of(chat) == {"quinoa": 2, "makhana-crunch": 2}, chat.keys


# ---------------------------------------------------------------- flags off, logging


def test_flags_off_keeps_the_name_search(plain, groq):
    onboard(plain)
    plain.send(PHONE)
    assert plain.keys[0] in ("search.results", "choose.product") and groq.calls == 0


def test_recommend_flag_off_keeps_the_name_search(backend, store, clock, groq):
    chat = make_chat(backend, store, clock, groq, recommend=False)
    onboard(chat)
    chat.send(PHONE)
    assert len(groq.extract_messages) == 0 and "recommend.intro" not in chat.keys


def test_the_log_line_has_constraint_names_and_no_customer_text(chat, groq, caplog):
    onboard(chat)
    groq.extract_queue.append(ext())
    with caplog.at_level(logging.INFO):
        chat.send("zebrafoo high protein under 777")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "zebrafoo" not in logs and "777" not in logs
    assert "stage=constraint" in logs and "cues=['price', 'protein_sort']" in logs
