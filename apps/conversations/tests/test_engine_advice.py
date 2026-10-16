"""Advice-style questions go to the recommendation flow, the advice line needs a real cue on every
route, and Hindi fractional prices are read. A scripted fake stands in for Groq: no network."""

from __future__ import annotations

import json
import logging

import pytest
from django.core.management import call_command

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

GYM = "What food do I need to take if I go to the gym regularly?"
HEADACHE = "I'm having a little headache, what do I have for dinner?"
DISCLAIMER = "I can't say what you personally need, but here is what's on our menu."


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


def ask(chat, groq, text, classification=None, **over):
    groq.extract_queue.append(ext(**over))
    if classification:
        groq.classify_queue.append(classification)
    chat.send(text)
    return picks(chat)


def products(chat, names):
    backend = chat.engine.backend
    found, offset = {}, 0
    while True:
        page, total = backend.list_products(limit=50, offset=offset)
        found.update({p.name: p for p in page})
        offset += len(page)
        if not page or offset >= total:
            return [found[n] for n in names]


# ---------------------------------------------------------------- A: the gym question


def test_the_gym_question_shows_the_advice_line_then_protein_sorted_picks(chat, groq):
    onboard(chat)
    shown = ask(chat, groq, GYM, asks_advice=True)
    assert chat.keys[:2] == ["advice.disclaimer", "recommend.intro"]
    assert chat.last[0].body == DISCLAIMER
    assert "Sorted by protein, highest first." in chat.message("recommend.intro").body
    proteins = [p.protein_g for p in products(chat, shown)]
    assert proteins and proteins == sorted(proteins, reverse=True)
    assert groq.calls == 1 and "search.none_neutral" not in chat.keys
    assert cart_of(chat) == {}


def test_the_gym_question_with_the_flags_off_gives_the_same_code_only_answer(plain):
    # Goal words are read in code: with no model at all the gym question still gets the advice
    # line and protein-sorted picks (it used to be a plain name search).
    onboard(plain)
    plain.send(GYM)
    assert plain.keys[:2] == ["advice.disclaimer", "recommend.intro"]
    assert "Sorted by protein, highest first." in plain.message("recommend.intro").body
    assert cart_of(plain) == {}


def test_recommend_flag_off_keeps_the_old_route(backend, store, clock, groq):
    chat = make_chat(backend, store, clock, groq, recommend=False)
    onboard(chat)
    chat.send(GYM)
    assert groq.extract_messages == [] and "recommend.intro" in chat.keys  # code-only picks


@pytest.mark.parametrize(
    "text",
    [
        "which protein is best for bulking",
        "what should I eat after a workout",
        "gym ke liye kya lun",
        "వర్కౌట్ తర్వాత ఏమి తినాలి",
    ],
)
def test_goal_plus_question_and_advice_cues_route_to_the_flow(chat, groq, text):
    onboard(chat)
    ask(chat, groq, text)
    assert chat.keys[0] in ("advice.disclaimer", "recommend.intro"), chat.keys
    assert groq.calls == 1 and "search.none_neutral" not in chat.keys


# ---------------------------------------------------------------- the exceptions


def test_a_named_category_keeps_its_category_behaviour(chat, groq, plain):
    onboard(chat)
    chat.send("what protein bowls do you have")
    assert groq.calls == 0 and "recommend.intro" not in chat.keys
    onboard(plain)
    plain.send("what protein bowls do you have")
    assert chat.keys == plain.keys  # the same route with the flags on and off


def test_a_named_product_still_finds_that_product(chat, groq):
    onboard(chat)
    chat.send("what food do I need quinoa")
    assert groq.calls == 0 and "advice.disclaimer" not in chat.keys
    assert "recommend.intro" not in chat.keys


def test_suggest_ragi_mix_is_unchanged(chat, groq):
    onboard(chat)
    chat.send("suggest ragi mix")
    assert groq.calls == 0 and "recommend.intro" not in chat.keys


def test_the_emergency_check_still_runs_first(chat, groq):
    onboard(chat)
    chat.send("what should I eat, I have chest pain")
    assert chat.keys == ["safety.emergency"] and groq.calls == 0


# ---------------------------------------------------------------- B: health words


def test_headache_plus_dinner_is_safety_then_meal_disclosure_then_browse(chat, groq):
    onboard(chat)
    ask(chat, groq, HEADACHE, health_context="symptom", asks_advice=True, meal_time="dinner")
    assert chat.keys == [
        "safety.medical",
        "safety.see_doctor",
        "meal_time.unsupported",
        "menu.body",
    ]
    assert picks(chat) == [] and cart_of(chat) == {}
    assert "advice.disclaimer" not in chat.keys


# ---------------------------------------------------------------- the advice line gate


def test_the_models_flag_alone_never_shows_the_line_on_any_route(backend, store, clock, groq):
    chat = make_chat(backend, store, clock, groq)
    onboard(chat)
    # cue route ("suggest"), classifier route (no cue word) and constraint route (a price)
    ask(chat, groq, "suggest zork florp", asks_advice=True, sort="protein_desc")
    assert chat.keys[0] == "recommend.intro"
    ask(chat, groq, "zork florp", cls("RECOMMEND"), asks_advice=True, sort="protein_desc")
    assert chat.keys[0] == "recommend.intro"
    ask(chat, groq, "Something high protein under 300", asks_advice=True)
    assert chat.keys[0] == "recommend.intro"
    ask(chat, groq, "300 ke andar high protein kuch", asks_advice=True)
    assert chat.keys[0] == "recommend.intro"


@pytest.mark.parametrize(
    "text, route",
    [
        ("suggest what should i eat", "cue"),
        ("what should i eat under 300", "constraint"),
        ("zork florp what should i eat", "classifier"),
    ],
)
def test_a_real_advice_cue_with_the_flag_shows_the_line(chat, groq, text, route):
    onboard(chat)
    ask(chat, groq, text, cls("RECOMMEND") if route == "classifier" else None, asks_advice=True)
    # With a price limit the advice line is followed by picks; with nothing to apply it is
    # followed by the category menu (never picks in catalog order).
    second = "recommend.intro" if route == "constraint" else "menu.body"
    assert chat.keys[:2] == ["advice.disclaimer", second]


def test_an_advice_cue_without_the_models_flag_shows_nothing(chat, groq):
    onboard(chat)
    ask(chat, groq, "suggest what should i eat", asks_advice=False)
    assert "advice.disclaimer" not in chat.keys


# ---------------------------------------------------------------- C: Hindi fractional prices


@pytest.mark.parametrize(
    "text, limit",
    [
        ("dhai sau ke andar protein", 250),
        ("dedh sau ke andar protein", 150),
        ("saade teen sau se kam protein", 350),
        ("saade paanch sau tak protein", 550),
        ("paune do sau ke andar protein", 175),
        ("ढाई सौ के अंदर प्रोटीन", 250),
        ("under dhai sau protein", 250),
    ],
)
def test_hindi_fractional_prices_are_read(chat, groq, text, limit):
    onboard(chat)
    shown = ask(chat, groq, text)
    items = products(chat, shown)
    assert "recommend.clarify" not in chat.keys and chat.keys[0] == "recommend.intro", chat.keys
    assert all(p.price <= limit for p in items)
    assert t("en", "recommend.max_price", max=limit) in chat.message("recommend.intro").body
    assert cart_of(chat) == {}


def test_the_phone_message_gets_max_250_and_protein_sort(chat, groq):
    onboard(chat)
    shown = ask(chat, groq, "dhai sau ke andar protein", confidence=0.4)  # even a doubtful model
    items = products(chat, shown)
    assert items and all(p.price <= 250 for p in items)
    proteins = [p.protein_g or 0 for p in items]
    assert proteins == sorted(proteins, reverse=True)
    assert "Showing items up to ₹250." in chat.message("recommend.intro").body
    assert "recommend.clarify" not in chat.keys


def test_a_fractional_price_is_never_a_quantity_flags_off(plain):
    onboard(plain)
    plain.send("dhai sau ke andar protein")
    assert cart_of(plain) == {} and "qty.invalid" not in plain.keys
    assert "qty.ask" not in plain.keys and "product.ask_qty" not in plain.keys


def test_a_comparator_with_no_readable_number_is_still_unreadable(chat, groq):
    onboard(chat)
    chat.send("something under")
    assert chat.keys == ["price.unreadable"] and groq.calls == 0


# ---------------------------------------------------------------- logs and the golden set


def test_logs_have_no_customer_text(chat, groq, caplog):
    onboard(chat)
    groq.extract_queue.append(ext(asks_advice=True))
    with caplog.at_level(logging.INFO):
        chat.send("zebrafoo what food do I need for the gym")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "zebrafoo" not in logs and "gym" not in logs


def test_the_advice_golden_set_runs_dry(settings, tmp_path):
    from apps.conversations.intent_eval import SET_NAMES, load_entries

    entries = load_entries("recommend_advice")
    assert "recommend_advice" in SET_NAMES and 18 <= len(entries) <= 25
    assert {e["language"] for e in entries} == {"en", "hi", "te", "hinglish", "tenglish"}
    assert all(e["needs_native_review"] for e in entries if e["language"] != "en")
    settings.IVA_DEV_RESET_ALLOWED = True
    out = tmp_path / "r.json"
    call_command("eval_intent_llm", golden_set="recommend_advice", dry_run=True, output=str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["dry_run"] is True and len(data["records"]) == len(entries)
    assert not any(r["failed"] for r in data["records"])
