"""The price, comparator and protein sort are read in code: the same message gives the same result
whatever the model returns and whatever state the conversation is in. A scripted fake stands in
for Groq: no network."""

from __future__ import annotations

import json
import logging

import pytest
from django.core.management import call_command

from apps.ai.classifier import ClassifierTimeout
from apps.ai.pipeline import IntentPipeline
from apps.conversations.engine import ConversationEngine

from .harness import Chat, onboard
from .test_engine_recommend import (  # noqa: F401  (local_cache is an autouse fixture)
    FakeGroq,
    cart_of,
    ext,
    local_cache,
    make_chat,
    picks,
)

PHONE = "dhai sau ke andar protein"
SORT_LINE = "Sorted by protein, highest first."
LIMIT_LINE = "Showing items up to ₹250."

# What the model might return for the same message (None = the call fails).
MODEL_REPLIES = {
    "product_question": lambda: ext(request_type="product_question"),
    "recommend_no_sort": lambda: ext(),
    "wrong_max_and_sort": lambda: ext(max_price=999, sort="price_asc"),
    "other_category": lambda: ext(category_slug="protein-bowls"),
    "other": lambda: ext(request_type="other"),
    "medical_no_health": lambda: ext(request_type="medical"),
    "invalid_json": lambda: "this is not json",
    "timeout": lambda: ClassifierTimeout(),
}


@pytest.fixture
def groq():
    return FakeGroq()


@pytest.fixture
def chat(backend, store, clock, groq):
    return make_chat(backend, store, clock, groq)


@pytest.fixture
def plain(backend, store, clock):
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


def get_to(chat, groq, state):
    if state == "category":
        chat.send("protein bowls")  # browse a category: the state is CATEGORY
    elif state == "pending":
        chat.send("add protein")  # a "which one?" list with a quantity question pending
        assert chat.keys == ["choose.product"]


def run(chat, groq, text, reply, state="menu"):
    onboard(chat)
    get_to(chat, groq, state)
    if reply is not None:
        groq.extract_queue.append(reply)
    chat.send(text)
    return chat


def check_phone(chat, limit=250, line=LIMIT_LINE):
    intro = chat.message("recommend.intro").body
    shown = products(chat, picks(chat))
    assert chat.keys[0] == "recommend.intro" and "recommend.clarify" not in chat.keys, chat.keys
    assert SORT_LINE in intro and line in intro, intro
    assert shown and all(p.price <= limit for p in shown)
    grams = [p.protein_g or 0 for p in shown]
    assert grams == sorted(grams, reverse=True)
    assert cart_of(chat) == {}


# ---------------------------------------------------------------- the phone sequence


def test_browse_protein_bowls_then_the_lowercase_message(chat, groq):
    onboard(chat)
    chat.send("protein bowls")
    assert chat.session().state.name == "CATEGORY"
    groq.extract_queue.append(ext(request_type="product_question"))  # what the live model said
    chat.send(PHONE)
    check_phone(chat)


@pytest.mark.parametrize("state", ["menu", "category", "pending"])
@pytest.mark.parametrize("text", [PHONE, "Dhai sau ke andar protein", "DHAI SAU KE ANDAR PROTEIN"])
def test_every_state_and_case_gives_max_250_and_the_protein_sort(chat, groq, state, text):
    run(chat, groq, text, ext(), state)
    check_phone(chat)


# ---------------------------------------------------------------- determinism


@pytest.mark.parametrize("state", ["menu", "category", "pending"])
@pytest.mark.parametrize("model", sorted(set(MODEL_REPLIES) - {"other_category"}))
def test_the_model_cannot_change_what_the_code_read(backend, store, clock, state, model):
    groq = FakeGroq()
    chat = make_chat(backend, store, clock, groq)
    run(chat, groq, PHONE, MODEL_REPLIES[model](), state)
    check_phone(chat)


def test_identical_picks_for_every_reply_that_adds_no_category(backend, store, clock):
    outcomes = set()
    for name, make in MODEL_REPLIES.items():
        if name == "other_category":
            continue  # the model may ADD a category: picks may then be narrower
        groq = FakeGroq()
        chat = make_chat(backend, store, clock, groq)
        run(chat, groq, PHONE, make())
        outcomes.add((tuple(picks(chat)), chat.message("recommend.intro").body))
    assert len(outcomes) == 1, outcomes


def test_a_category_the_model_adds_is_applied_on_top_of_the_code_read_limit(chat, groq):
    # the model MAY add a category; no Protein Bowl costs 250 or less, so nothing qualifies and
    # the existing no-match reply is shown (never a bowl above the limit, never the clarify reply)
    run(chat, groq, PHONE, MODEL_REPLIES["other_category"]())
    assert chat.keys == ["recommend.none"] and cart_of(chat) == {}


def test_with_no_model_call_at_all_the_result_is_the_same(chat, groq, plain):
    run(chat, groq, PHONE, None, "pending")  # a pending choice: the model is not called
    assert groq.calls == 0
    check_phone(chat)


def test_a_model_diet_or_sort_never_replaces_the_code_read_ones(chat, groq):
    run(chat, groq, "veg dhai sau ke andar protein", ext(diet_type="non_veg", sort="price_desc"))
    shown = products(chat, picks(chat))
    assert shown and all(p.diet_type == "veg" and p.price <= 250 for p in shown)
    assert SORT_LINE in chat.message("recommend.intro").body


# ---------------------------------------------------------------- other fractions, never a quantity


@pytest.mark.parametrize(
    "text, limit",
    [
        ("dedh sau ke andar protein", 150),
        ("saade teen sau se kam protein", 350),
        ("saade paanch sau tak protein", 550),
        ("paune do sau ke andar protein", 175),
        ("ढाई सौ के अंदर प्रोटीन", 250),
        ("डेढ़ सौ तक प्रोटीन", 150),
        ("साढ़े तीन सौ से कम प्रोटीन", 350),
        ("पौने दो सौ के अंदर प्रोटीन", 175),
    ],
)
@pytest.mark.parametrize("model", ["product_question", "recommend_no_sort", "timeout"])
def test_fractional_forms_are_read_whatever_the_model_says(
    backend, store, clock, text, limit, model
):
    groq = FakeGroq()
    chat = make_chat(backend, store, clock, groq)
    run(chat, groq, text, MODEL_REPLIES[model]())
    check_phone(chat, limit, f"Showing items up to ₹{limit}.")


@pytest.mark.parametrize(
    "text", [PHONE, "dedh sau ke andar protein", "paune do sau ke andar protein"]
)
def test_a_fractional_price_is_never_a_quantity_flags_off(plain, text):
    onboard(plain)
    plain.send(text)
    assert cart_of(plain) == {} and "recommend.intro" not in plain.keys
    assert not {"qty.invalid", "qty.ask", "product.ask_qty"} & set(plain.keys)


# ---------------------------------------------------------------- zero picks, flags, logs


@pytest.mark.parametrize("model", ["product_question", "invalid_json", "timeout"])
def test_a_price_cue_with_no_picks_is_the_no_match_reply_never_the_generic_clarify(
    backend, store, clock, model
):
    groq = FakeGroq()
    chat = make_chat(backend, store, clock, groq)
    run(chat, groq, "protein under 10", MODEL_REPLIES[model]())
    assert chat.keys == ["recommend.none"] and cart_of(chat) == {}


def test_flags_off_keeps_the_old_route(plain):
    onboard(plain)
    plain.send(PHONE)
    assert "recommend.intro" not in plain.keys


def test_the_recommend_line_says_which_constraints_came_from_code(chat, groq, caplog):
    onboard(chat)
    groq.extract_queue.append(ext())
    with caplog.at_level(logging.INFO):
        chat.send("zebrafoo " + PHONE)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "code=['max_price', 'sort'] model=[]" in logs
    assert "zebrafoo" not in logs and "dhai" not in logs and "250" not in logs


def test_a_constraint_only_the_model_found_is_marked_as_the_models(chat, groq, caplog):
    onboard(chat)
    groq.extract_queue.append(ext(category_slug="protein-bowls"))
    with caplog.at_level(logging.INFO):
        chat.send(PHONE)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "code=['max_price', 'sort'] model=['category_slug']" in logs


def test_the_determinism_golden_set_runs_dry(settings, tmp_path):
    from apps.conversations.intent_eval import SET_NAMES, load_entries

    entries = load_entries("recommend_determinism")
    assert "recommend_determinism" in SET_NAMES and 14 <= len(entries) <= 20
    assert {e["language"] for e in entries} == {"en", "hi", "te", "hinglish", "tenglish"}
    assert all(e["needs_native_review"] for e in entries if e["language"] != "en")
    settings.IVA_DEV_RESET_ALLOWED = True
    out = tmp_path / "r.json"
    call_command(
        "eval_intent_llm", golden_set="recommend_determinism", dry_run=True, output=str(out)
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["dry_run"] is True and len(data["records"]) == len(entries)
    assert all(r["route"] == "constraint" and not r["failed"] for r in data["records"])
