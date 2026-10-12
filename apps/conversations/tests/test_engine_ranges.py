"""Price ranges, spelled-out prices, unreadable prices and the advice line, through the engine.
A scripted fake stands in for Groq: no network."""

from __future__ import annotations

import logging

import pytest

from apps.ai.pipeline import IntentPipeline
from apps.conversations.engine import ConversationEngine
from apps.conversations.messages import t

from .harness import Chat, onboard
from .test_engine_recommend import (  # noqa: F401  (local_cache is an autouse fixture)
    FakeGroq,
    cart_of,
    cls,
    ext,
    local_cache,
    make_chat,
    picks,
)

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


def products(chat, names):
    backend = chat.engine.backend
    found, offset = {}, 0
    while True:
        page, total = backend.list_products(limit=50, offset=offset)
        found.update({p.name: p for p in page})
        offset += len(page)
        if not page or offset >= total:
            return [found[n] for n in names]


def ask(chat, groq, text, **over):
    groq.extract_queue.append(ext(**over))
    chat.send(text)
    return picks(chat)


# ---------------------------------------------------------------- ranges


def test_the_phone_range_shows_only_items_inside_it_sorted_by_protein(chat, groq):
    onboard(chat)
    shown = ask(chat, groq, "High protein 250-300")
    items = products(chat, shown)
    assert items and all(250 <= p.price <= 300 for p in items)
    proteins = [p.protein_g for p in items]
    assert proteins == sorted(proteins, reverse=True)
    assert not {"Garlic Steak & Prawns Bowl", "Grilled Chicken Quinoa Bowl"} & set(shown)
    assert t("en", "recommend.price_range", min=250, max=300) in (
        chat.message("recommend.intro").body
    )
    assert cart_of(chat) == {} and "choose.product" not in chat.keys


@pytest.mark.parametrize(
    "text",
    [
        "250 to 300 high protein",
        "between 250 and 300 veg",
        "Rs 250-300 snacks",
        "₹250 to ₹300 veg",
        "250 se 300 tak high protein",
        "250 nundi 300 varaku high protein",
        "300-250 rupees high protein",
        "250 - 300 veg",
        "250 से 300 तक हाई प्रोटीन",
        "250 నుండి 300 వరకు హై ప్రోటీన్",
    ],
)
def test_range_forms_apply_both_bounds_strictly(chat, groq, text):
    onboard(chat)
    shown = ask(chat, groq, text)  # the model returns no price at all: the code reads it
    items = products(chat, shown)
    assert items, chat.keys
    assert all(250 <= p.price <= 300 for p in items), [(p.name, p.price) for p in items]
    assert (
        t("en", "recommend.price_range", min=250, max=300) in chat.message("recommend.intro").body
    )
    assert cart_of(chat) == {}


def test_the_model_returning_wrong_bounds_loses_to_the_message(chat, groq):
    onboard(chat)
    shown = ask(chat, groq, "High protein 250-300", min_price=1, max_price=900)
    assert all(250 <= p.price <= 300 for p in products(chat, shown))


def test_a_min_only_price_is_applied_and_stated(chat, groq):
    onboard(chat)
    shown = ask(chat, groq, "above Rs 800 high protein")
    items = products(chat, shown)
    assert items and all(p.price >= 800 for p in items)
    assert t("en", "recommend.min_price", min=800) in chat.message("recommend.intro").body


def test_a_range_with_nothing_inside_uses_the_existing_no_match_reply(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext())
    chat.send("high protein 10-20")
    assert chat.keys == ["recommend.none"]


def test_the_range_line_is_translated(backend, store, clock, groq):
    chat = make_chat(backend, store, clock, groq)
    onboard(chat, lang="te")
    ask(chat, groq, "High protein 250-300")
    body = chat.message("recommend.intro").body
    assert t("te", "recommend.price_range", min=250, max=300) in body
    assert t("te", "recommend.price_range", min=250, max=300) != t(
        "en", "recommend.price_range", min=250, max=300
    )


# ---------------------------------------------------------------- spelled-out prices


@pytest.mark.parametrize(
    "text, limit",
    [
        ("Something under two hundred", 200),
        ("below two fifty", 250),
        ("under one thousand", 1000),
        ("under three hundred and fifty", 350),
        ("do sau ke andar", 200),
        ("రెండు వందలు లోపు", 200),
    ],
)
def test_spelled_out_limits_are_read_and_never_a_quantity(chat, groq, text, limit):
    onboard(chat)
    shown = ask(chat, groq, text)
    items = products(chat, shown)
    assert items and all(p.price <= limit for p in items)
    assert t("en", "recommend.max_price", max=limit) in chat.message("recommend.intro").body
    assert cart_of(chat) == {} and BAD_QUANTITY not in chat.keys
    assert "search.none_neutral" not in chat.keys


@pytest.mark.parametrize("text", ["Something under two hundred", "below two fifty"])
def test_spelled_out_limits_flags_off_are_never_a_quantity(plain, text):
    onboard(plain)
    plain.send(text)
    assert cart_of(plain) == {} and BAD_QUANTITY not in plain.keys
    assert "qty.ask" not in plain.keys and "product.ask_qty" not in plain.keys


# ---------------------------------------------------------------- pairs with no cue


@pytest.mark.parametrize("text", ["snacks 15-20", "protein 10-20"])
@pytest.mark.parametrize("flags", ["on", "off"])
def test_a_plain_hyphen_pair_never_adds_or_becomes_a_quantity(
    backend, store, clock, groq, text, flags
):
    chat = (
        make_chat(backend, store, clock, groq)
        if flags == "on"
        else Chat(ConversationEngine(backend, store, IntentPipeline(), clock=clock))
    )
    onboard(chat)
    if flags == "on":
        groq.classify_queue.append("{}")
    chat.send(text)
    ids = [
        i for i in chat.ids if i.startswith(("PRODUCT_SELECT:", "ADD_TO_CART:", "ACT:showcard:"))
    ]
    for reply_id in ids[:5]:
        chat.send(text)
        chat.tap(reply_id)
        assert BAD_QUANTITY not in chat.keys and "cart.added" not in chat.keys, chat.keys
    assert cart_of(chat) == {}


def test_add_with_a_hyphen_pair_is_unchanged_with_the_flags_on_or_off(
    backend, store, clock, groq, plain
):
    onboard(plain)
    plain.send("add 2-3 quinoa")
    chat = make_chat(backend, store, clock, groq)
    onboard(chat)
    chat.send("add 2-3 quinoa")
    assert groq.calls == 0 and chat.keys == plain.keys


# ---------------------------------------------------------------- unreadable prices


@pytest.mark.parametrize("text", ["something under", "under rupees", "snacks within rs"])
def test_a_price_word_with_no_number_gets_price_unreadable_and_no_picks(chat, groq, text):
    onboard(chat)
    chat.send(text)
    assert chat.keys == ["price.unreadable"] and groq.calls == 0
    assert chat.last[0].body == t("en", "price.unreadable")
    assert "Menu" in chat.last[0].buttons[0].title or chat.last[0].buttons
    assert "search.none_neutral" not in chat.keys and picks(chat) == []
    assert cart_of(chat) == {}


def test_price_unreadable_is_translated(backend, store, clock, groq):
    chat = make_chat(backend, store, clock, groq)
    onboard(chat, lang="hi")
    chat.send("something under")
    assert chat.last[0].body == t("hi", "price.unreadable") != t("en", "price.unreadable")


# ---------------------------------------------------------------- the advice line


@pytest.mark.parametrize(
    "text", ["Something high protein under 300", "300 ke andar high protein kuch"]
)
def test_a_plain_request_never_shows_the_advice_line(chat, groq, text):
    onboard(chat)
    ask(chat, groq, text, asks_advice=True, sort="protein_desc")  # the model's flag alone
    assert "advice.disclaimer" not in chat.keys and chat.keys[0] == "recommend.intro"


def test_a_real_advice_question_still_shows_it(chat, groq):
    onboard(chat)
    ask(chat, groq, "what should I eat under 300", asks_advice=True)
    assert chat.keys[:2] == ["advice.disclaimer", "recommend.intro"]


def test_the_gym_question_still_shows_it(chat, groq):
    onboard(chat)
    # a recommendation cue ("suggest") sends it to the model; the advice cue is in the text
    text = "suggest what food do I need to take if I go to the gym regularly"
    ask(chat, groq, text, asks_advice=True)
    assert chat.keys[:2] == ["advice.disclaimer", "recommend.intro"]


# ---------------------------------------------------------------- logs


def test_the_log_line_names_the_constraints_and_never_the_text(chat, groq, caplog):
    onboard(chat)
    groq.extract_queue.append(ext())
    with caplog.at_level(logging.INFO):
        chat.send("zebrafoo high protein 250-300")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "zebrafoo" not in logs and "250" not in logs and "300" not in logs
    assert "cues=['price_range', 'protein_sort']" in logs


def test_the_unreadable_reply_logs_only_its_name(chat, groq, caplog):
    onboard(chat)
    with caplog.at_level(logging.INFO):
        chat.send("zebrafoo under")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "zebrafoo" not in logs and "price_unreadable" in logs
