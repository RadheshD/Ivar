"""Health-aware recommendations in the engine, emergency check, fuzzy cart guard.

A scripted fake stands in for Groq; nothing reaches the network. The model supplies
constraints only: every word the customer sees comes from templates.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from django.core.cache.backends.locmem import LocMemCache

from apps.ai import llm_guard
from apps.ai.meal_time_map import MEAL_TIME_CATEGORIES
from apps.conversations.channel import Inbound, OutButtons
from apps.conversations.intent_eval import Harness, canned_reply, load_entries
from apps.conversations.messages import CATALOGS, t

from .harness import Chat, onboard
from .test_engine_recommend import FakeGroq, cart_of, cls, ext
from .test_engine_recommend_priority import bodies, build, names

CUE = "suggest zork florp"  # a recommendation cue the rules do not understand
EMERGENCY_REPLY = (
    "This sounds serious. Please contact a doctor or your local emergency services right now."
)


@pytest.fixture(autouse=True)
def local_cache(monkeypatch):
    monkeypatch.setattr(llm_guard, "cache", LocMemCache(f"health-{uuid.uuid4().hex}", {}))


@pytest.fixture
def groq():
    return FakeGroq()


def ask(chat, groq, text, **fields):
    groq.extract_queue.append(ext(**fields))
    return chat.send(text)


@pytest.fixture
def chat(backend, store, clock, groq):
    return build(backend, store, clock, groq)


@pytest.fixture
def plain(backend, store, clock, groq):
    """No LLM layer at all: the previous behaviour (plus the always-on safety check)."""
    return build(backend, store, clock, groq, with_llm=False)


# ---------------------------------------------------------------- the golden cases


def test_every_health_golden_case_matches_the_real_decision_code():
    harness = Harness()
    for entry in load_entries("recommend_health"):
        record = harness.evaluate(
            entry, "recommend_health", canned_reply(entry, "recommend_health"), None
        )
        assert record["safety"] == [], entry["id"]
        assert record["action_class"] == entry["action_class"], (entry["id"], record["reply_keys"])
        want = entry["reply_keys"]
        assert record["reply_keys"][: len(want)] == want, (entry["id"], record["reply_keys"])
        assert not record["failed"], entry["id"]


def test_the_health_golden_set_is_well_formed():
    entries = load_entries("recommend_health")
    assert 38 <= len(entries) <= 50 and len({e["id"] for e in entries}) == len(entries)
    assert {e["language"] for e in entries} >= {"en", "hi", "te", "hinglish", "tenglish", "mixed"}
    tags = {tag for e in entries for tag in e["tags"]}
    assert {"gym", "headache", "weight_loss", "diabetes", "pregnancy", "kids", "nut_free",
            "ingredient", "popularity", "diet_plan", "emergency", "self_harm", "meal_time",
            "followup", "cue_with_product", "injection", "low_calorie"} <= tags  # fmt: skip
    assert all(e["needs_native_review"] for e in entries if e["language"] != "en")


# ---------------------------------------------------------------- composition


def test_headache_plus_dinner_is_safety_then_meal_disclosure_then_browse(chat, groq):
    onboard(chat)
    ask(chat, groq, "I'm having a little headache, what do I have for dinner?",
        health_context="symptom", asks_advice=True, meal_time="dinner")  # fmt: skip
    assert chat.keys == [
        "safety.medical",
        "safety.see_doctor",
        "meal_time.unsupported",
        "menu.body",
    ]
    assert chat.last[0].body == "I can't give medical or dietary advice."
    assert chat.last[1].body == "If you feel unwell, please see a doctor."
    assert (
        chat.last[2].body
        == "I can't filter by meal time yet, so here is the full menu by category."
    )
    assert names(chat) == [] and groq.calls == 1
    text = " ".join(bodies(chat)).lower()
    assert "headache" not in text and "dinner" not in text  # nothing links food to the symptom


@pytest.mark.parametrize("health", ["symptom", "condition", "pregnancy_or_child"])
def test_symptom_condition_and_pregnancy_get_both_safety_lines(chat, groq, health):
    onboard(chat)
    ask(chat, groq, CUE, health_context=health)
    assert chat.keys[:3] == ["safety.medical", "safety.see_doctor", "menu.body"]
    assert names(chat) == []


def test_weight_loss_gets_only_the_medical_line_and_never_a_calorie_sort(chat, groq, backend):
    onboard(chat)
    ask(chat, groq, "suggest zork florp", health_context="weight_loss", sort="calories_asc")
    assert chat.keys == ["safety.medical", "menu.body"]  # no doctor line, no picks, no sort
    assert chat.last[0].body == "I can't give medical or dietary advice."


def test_weight_loss_with_an_explicit_low_calorie_request_shows_real_calories(chat, groq, backend):
    onboard(chat)
    ask(chat, groq, "suggest zork florp low calorie options", health_context="weight_loss",
        sort="calories_asc")  # fmt: skip
    assert chat.keys[:2] == ["safety.medical", "recommend.intro"]
    by_name = {p.name: p for p in backend._products.values()}
    kcal = [by_name[n].calories_kcal for n in names(chat)]
    assert kcal == sorted(kcal)
    assert all("kcal" in b for b in [m.body for m in chat.last if m.key == "recommend.card"])


def test_allergy_gets_the_allergen_line_and_no_picks(chat, groq):
    onboard(chat)
    ask(chat, groq, CUE, health_context="allergy", category_slug="nuts-seeds-snacks")
    assert chat.keys == ["safety.allergy", "menu.body"]
    assert chat.last[0].body == (
        "I don't have allergen information for these products. Please check with the Ivar team."
    )
    assert names(chat) == []


def test_health_wording_never_changes_the_products_or_their_order(clock):
    from apps.conversations.fake_backend import FakeBackend
    from apps.conversations.state import InMemorySessionStore

    from .test_engine_recommend import FakeGroq

    shown = {}
    for health in ("none", "symptom", "condition", "pregnancy_or_child"):
        g = FakeGroq()
        chat = build(FakeBackend(state_file=""), InMemorySessionStore(), clock, g)
        onboard(chat)
        g.extract_queue.append(
            ext(
                health_context=health, category_slug="protein-active-nutrition", sort="protein_desc"
            )
        )
        chat.send("suggest zork florp protein")
        shown[health] = names(chat)
    assert shown["none"] and all(v == shown["none"] for v in shown.values())


def test_a_sort_the_message_did_not_ask_for_is_dropped_under_health_wording(chat, groq, backend):
    onboard(chat)
    ask(chat, groq, "suggest zork florp for my headache", health_context="symptom",
        category_slug="nuts-seeds-snacks", sort="calories_asc")  # fmt: skip
    shown = names(chat)
    assert chat.keys[:2] == ["safety.medical", "safety.see_doctor"] and shown
    in_category = [
        p.name for p in backend._products.values() if p.category_id == "nuts-seeds-snacks"
    ]
    assert shown == in_category[: len(shown)]  # catalog order, not a calorie ranking


def test_asks_advice_adds_the_disclaimer_before_the_picks(chat, groq):
    onboard(chat)
    ask(chat, groq, "suggest what should i eat", asks_advice=True, sort="protein_desc")
    assert chat.keys[:2] == ["advice.disclaimer", "recommend.intro"]
    assert (
        chat.last[0].body == "I can't say what you personally need, but here is what's on our menu."
    )
    ask(chat, groq, "suggest what should i eat", asks_advice=False, sort="protein_desc")
    assert "advice.disclaimer" not in chat.keys


def test_picks_after_safety_need_explicit_food_constraints(chat, groq):
    onboard(chat)
    ask(chat, groq, "suggest zork florp veg", health_context="symptom",
        diet_type="veg", max_price=300)  # fmt: skip
    assert chat.keys[:3] == ["safety.medical", "safety.see_doctor", "recommend.intro"]
    assert names(chat)


def test_no_template_links_a_food_to_a_symptom_or_condition():
    banned = (
        "cure",
        "heal",
        "treat",
        "relief",
        "boost",
        "helps",
        "good for your",
        "benefit",
        "need",
    )
    for lang, catalog in CATALOGS.items():
        for key in ("safety.medical", "safety.see_doctor", "safety.allergy", "safety.emergency",
                    "meal_time.unsupported", "popularity.unsupported"):  # fmt: skip
            text = catalog[key].lower()
            assert not any(word in text for word in banned), (lang, key)
    # the disclaimer talks about what the customer "needs" only to refuse to say it
    assert "can't say" in CATALOGS["en"]["advice.disclaimer"]


# ---------------------------------------------------------------- unsupported constraints


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("ingredient", "ingredients"),
        ("allergen", "allergens"),
        ("gluten_free", "gluten-free"),
        ("sugar_free", "sugar-free"),
        ("vegan", "vegan"),
        ("other_label", "that label"),
    ],
)
def test_unsupported_constraints_are_disclosed(chat, groq, value, label):
    onboard(chat)
    ask(chat, groq, CUE, unsupported_constraint=value)
    assert chat.last[0].key == "recommend.unsupported"
    assert (
        "I can't filter by" in chat.last[0].body
        and "yet, so these are the closest options" in chat.last[0].body
    )
    assert names(chat)  # the closest options are still shown


def test_popularity_is_declined_with_the_fixed_sentence(chat, groq):
    onboard(chat)
    ask(chat, groq, "suggest zork florp best seller", unsupported_constraint="popularity")
    assert chat.keys == ["popularity.unsupported", "menu.body"]
    assert chat.last[0].body == "I don't have sales or popularity data."


def test_empty_meal_time_map_gives_the_disclosure_and_applies_other_constraints(chat, groq):
    onboard(chat)
    ask(chat, groq, "suggest zork florp veg dinner", meal_time="dinner", diet_type="veg")
    assert chat.keys[:2] == ["meal_time.unsupported", "recommend.intro"]
    ask(chat, groq, "suggest zork florp dinner", meal_time="dinner")
    assert chat.keys == ["meal_time.unsupported", "menu.body"]  # nothing else asked: the menu


def test_a_filled_meal_time_map_is_a_plain_category_filter(chat, groq, backend, monkeypatch):
    monkeypatch.setitem(MEAL_TIME_CATEGORIES, "dinner", ["protein-bowls"])
    onboard(chat)
    ask(chat, groq, "suggest zork florp dinner", meal_time="dinner")
    by_name = {p.name: p for p in backend._products.values()}
    assert "meal_time.unsupported" not in chat.keys
    assert names(chat) and all(by_name[n].category_id == "protein-bowls" for n in names(chat))


def test_followups_are_fresh_messages_with_the_neutral_reply(chat, groq):
    onboard(chat)
    ask(chat, groq, "cheaper ones")
    assert chat.keys == ["search.none_neutral", "menu.body"]
    assert chat.last[0].body == "I couldn't find that on the menu. You can browse instead."
    assert "cheaper" not in " ".join(bodies(chat))


# ---------------------------------------------------------------- emergencies


@pytest.mark.parametrize(
    "text",
    ["I have chest pain", "I think it's a heart attack", "I want to kill myself", "सीने में दर्द है"],
)
def test_emergency_keywords_get_only_the_emergency_reply_and_no_model_call(chat, groq, text):
    onboard(chat)
    chat.send(text)
    # self-harm wording has its own template key (the same text until a reviewer supplies wording)
    key = "safety.self_harm" if "kill myself" in text else "safety.emergency"
    assert chat.keys == [key] and groq.calls == 0
    assert chat.last[0].body == EMERGENCY_REPLY  # the customer's saved language is English
    assert [b.id for b in chat.last[0].buttons] == ["CATEGORY_SELECT"]  # a Menu button only
    assert names(chat) == [] and cart_of(chat) == {}


def test_emergency_check_works_with_every_llm_flag_off(plain, groq):
    onboard(plain)
    plain.send("chest pain and I can't breathe")
    assert plain.keys == ["safety.emergency"]
    assert plain.last[0].body == EMERGENCY_REPLY
    plain.send("I have a headache")  # not an emergency: the previous behaviour
    assert plain.keys == ["fallback"] or plain.keys[0] != "safety.emergency"


def test_emergency_reply_appends_the_owners_contact_text_only_if_set(chat, settings):
    onboard(chat)
    chat.send("chest pain")
    assert chat.last[0].body == EMERGENCY_REPLY  # nothing invented by default
    settings.IVA_EMERGENCY_CONTACT_TEXT = "Call [OWNER-SUPPLIED NUMBER]."
    chat.send("chest pain")
    assert chat.last[0].body == EMERGENCY_REPLY + "\nCall [OWNER-SUPPLIED NUMBER]."


def test_emergency_reply_follows_the_customers_language_with_english_as_default(
    backend, store, clock, groq
):
    chat = build(backend, store, clock, groq)
    onboard(chat, lang="te")
    chat.send("chest pain")
    assert chat.last[0].body == t("te", "safety.emergency") != t("en", "safety.emergency")
    fresh = Chat(chat.engine, "919800000009")  # never onboarded: language unknown
    fresh.send("heart attack")
    assert fresh.last[0].body == EMERGENCY_REPLY


def test_emergency_check_ignores_button_replies_and_can_be_switched_off(chat, groq, settings):
    onboard(chat)
    chat.last = chat.engine.handle(
        chat.whatsapp_id, Inbound(reply_id="ACT:more", reply_title="chest pain")
    )
    assert "safety.emergency" not in chat.keys
    settings.IVA_SAFETY_KEYWORDS_ENABLED = False
    chat.send("chest pain")
    assert "safety.emergency" not in chat.keys


def test_emergency_check_runs_in_the_middle_of_checkout_and_onboarding(chat, groq):
    chat.send("hello")  # onboarding: language prompt
    chat.send("heart attack")
    assert chat.keys == ["safety.emergency"]
    onboard(chat)
    chat.send("add 2 makhana crunch")
    chat.send("checkout")
    chat.send("chest pain")  # at the address step
    assert chat.keys == ["safety.emergency"]
    assert cart_of(chat) == {"makhana-crunch": 2}  # nothing else changed


def test_the_model_can_also_flag_an_emergency(chat, groq):
    onboard(chat)
    ask(chat, groq, "suggest zork florp", health_context="emergency", confidence=0.2)
    assert chat.keys == ["safety.emergency"] and names(chat) == [] and cart_of(chat) == {}


def test_emergency_logs_stage_and_language_only(chat, caplog):
    onboard(chat)
    with caplog.at_level(logging.INFO):
        chat.send("I want to kill myself")
    line = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("engine: safety")
    )
    assert line == "engine: safety stage=safety kind=self_harm language=en"
    assert "kill" not in caplog.text


# ---------------------------------------------------------------- fuzzy cart guard


def test_a_fuzzy_cart_change_asks_yes_no_and_changes_nothing_until_yes(plain):
    onboard(plain)
    plain.send("makhna crunch 3")  # a typo in the product name: the fuzzy stage
    assert plain.keys == ["llm.confirm_add"] and cart_of(plain) == {}
    assert plain.last[0].body == "Add 3 × Makhana Crunch?"
    assert plain.ids == ["ACT:llmyes", "ACT:llmno"]
    plain.tap("ACT:llmyes")
    assert cart_of(plain) == {"makhana-crunch": 3}


def test_no_cancels_and_a_stale_yes_does_nothing(plain):
    onboard(plain)
    plain.send("makhna crunch 3")
    plain.tap("ACT:llmno")
    assert cart_of(plain) == {} and plain.keys == ["llm.cancelled"]
    plain.tap("ACT:llmyes")
    assert cart_of(plain) == {}


def test_typing_something_else_cancels_a_fuzzy_confirmation(plain):
    onboard(plain)
    plain.send("makhna crunch 3")
    plain.send("show my cart")
    plain.tap("ACT:llmyes")
    assert cart_of(plain) == {}


def test_rules_stage_adds_are_unchanged_and_immediate(plain):
    onboard(plain)
    plain.send("ad 2 makhana crunch")
    assert cart_of(plain) == {"makhana-crunch": 2} and "cart.added" in plain.keys
    plain.send("add 2 chia seeds")
    assert cart_of(plain)["chia-seeds"] == 2


def test_fuzzy_change_with_no_unique_product_or_no_quantity_still_asks_the_customer(plain):
    onboard(plain)
    plain.send("makhna crunch")  # no quantity: the existing flow asks how many
    assert "llm.confirm_add" not in plain.keys and cart_of(plain) == {}


def test_the_confirmation_can_be_switched_off_by_setting(plain, settings):
    settings.IVA_FUZZY_CART_CONFIRM = False
    onboard(plain)
    plain.send("makhna crunch 3")
    assert cart_of(plain) == {"makhana-crunch": 3}  # the old immediate behaviour


def test_fuzzy_never_makes_a_quantity_out_of_weight_even_next_to_a_real_product(plain, settings):
    settings.IVA_FUZZY_NUMBER_GUARD = True
    onboard(plain)
    plain.send("what'd you recommend, I want to lose my body weight, makhana crunch")
    assert cart_of(plain) == {}
    assert not any(
        "8" in getattr(m, "body", "") and "×" in getattr(m, "body", "") for m in plain.last
    )


def test_even_with_the_number_guard_off_a_fuzzy_quantity_needs_yes(plain):
    onboard(plain)
    plain.send("weight makhna crunch")  # may read as a quantity: it still asks first
    assert cart_of(plain) == {}


# ------------------------------------------------ never a cart change, never model text


def test_recommendations_and_health_replies_never_change_the_cart(chat, groq):
    onboard(chat)
    ask(chat, groq, CUE, health_context="symptom", category_slug="nuts-seeds-snacks")
    ask(chat, groq, CUE, health_context="allergy")
    ask(chat, groq, CUE, asks_advice=True, sort="protein_desc")
    ask(chat, groq, CUE, unsupported_constraint="popularity")
    assert cart_of(chat) == {}
    assert not any(k and k.startswith("llm.confirm") for k in chat.keys)


def test_no_model_text_reaches_the_customer(chat, groq):
    onboard(chat)
    ask(chat, groq, CUE, health_context="symptom", product_mention="turmeric cures headache",
        category_slug="nuts-seeds-snacks", asked_fact="cures everything")  # fmt: skip
    text = " ".join(bodies(chat)).lower()
    assert "turmeric" not in text and "cures" not in text


def test_flags_off_keeps_the_previous_behaviour_apart_from_the_safety_check(
    backend, store, clock, groq
):
    chat = build(backend, store, clock, groq, recommend=False)  # LLM layer on, recommend off
    onboard(chat)
    groq.classify_queue.append(cls("unknown"))
    chat.send("I'm having a little headache, what do I have for dinner?")
    assert chat.keys == ["search.none_neutral"] and groq.extract_messages == []  # as before
    chat.send("chest pain")  # the one thing that is always on
    assert chat.keys == ["safety.emergency"]


def test_no_customer_text_in_logs(chat, groq, caplog):
    onboard(chat)
    with caplog.at_level(logging.INFO):
        ask(chat, groq, "private words about my headache tonight", health_context="symptom")
    assert "private words" not in caplog.text and "headache tonight" not in caplog.text
    line = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("engine: recommend")
    )
    assert "health=symptom" in line and "outcome=safety_block" in line


def test_the_new_templates_exist_in_all_three_languages():
    keys = ["safety.medical", "safety.see_doctor", "safety.allergy", "advice.disclaimer",
            "safety.emergency", "meal_time.unsupported", "popularity.unsupported",
            "recommend.what.ingredient", "recommend.what.allergen"]  # fmt: skip
    for lang in ("en", "te", "hi"):
        for key in keys:
            assert CATALOGS[lang][key]
    assert OutButtons  # the emergency reply is a buttons message with a single Menu button
