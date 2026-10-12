"""Phone-testing fixes: cue priority, no echoed fragments, weight loss -> medical, goal -> sort.

Mocked Groq only (a scripted fake); nothing reaches the network.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from django.core.cache.backends.locmem import LocMemCache

from apps.ai import llm_guard
from apps.ai.intents import RecommendRequest
from apps.ai.llm_fallback import LLMFallback
from apps.ai.llm_guard import CircuitBreaker, LLMGuard
from apps.ai.pipeline import IntentPipeline
from apps.ai.recommend import apply_goal_sort, cue_matches, goal_sort, medical_matches
from apps.ai.recommend_cues import GOAL_SORT
from apps.conversations.engine import ConversationEngine
from apps.conversations.messages import t

from .harness import Chat, onboard
from .test_engine_recommend import FakeGroq, cart_of, cls, ext, picks

PHONE_A = "Suggest me some good meals for the gym tonight"
PHONE_B = "What'd you recommend me if I want to lose my body weight?"
MEDICAL_REPLY = (
    "I can't give medical or dietary advice, but here is what's on our menu, "
    "with the details we have."
)


@pytest.fixture(autouse=True)
def local_cache(monkeypatch):
    monkeypatch.setattr(llm_guard, "cache", LocMemCache(f"prio-{uuid.uuid4().hex}", {}))


@pytest.fixture
def groq():
    return FakeGroq()


def build(backend, store, clock, groq, *, recommend=True, with_llm=True):
    pipeline = IntentPipeline()
    if with_llm:
        guard = LLMGuard(daily_cap=100, per_customer_per_minute=0, breaker=CircuitBreaker(3, 60))
        pipeline = IntentPipeline(
            fallback=LLMFallback(groq, guard, min_confidence=0.7, timeout=2, recommend=recommend)
        )
    return Chat(ConversationEngine(backend, store, pipeline, clock=clock))


@pytest.fixture
def chat(backend, store, clock, groq):
    return build(backend, store, clock, groq)


def bodies(chat):
    return [m.body for m in chat.last if getattr(m, "body", None)]


def names(chat):
    return picks(chat)


def protein_of(backend, name):
    return next(p.protein_g for p in backend._products.values() if p.name == name)


# ---------------------------------------------------------------- message B: medical


def test_message_b_gets_the_medical_reply_with_the_menu_and_no_model_call(chat, groq):
    onboard(chat)
    chat.send(PHONE_B)
    assert chat.keys == ["recommend.medical", "menu.body"]
    assert chat.last[0].body == MEDICAL_REPLY
    assert groq.calls == 0 and names(chat) == []
    assert not any("recommend if lose" in b for b in bodies(chat))


@pytest.mark.parametrize(
    "text",
    [
        "what do you suggest for weight loss",
        "recommend a diet for fat loss",
        "I want to get slim, any suggestions",
        "how can I lose weight",
        "वजन कम करने के लिए क्या सुझाओगे",
        "బరువు తగ్గడానికి ఏం సూచిస్తారు",
        "weight kam karne ke liye kya suggest karoge",
        "baruvu taggadaniki emi suggest cheyyi",
    ],
)
def test_weight_loss_in_every_language_is_medical_without_a_model_call(chat, groq, text):
    onboard(chat)
    chat.send(text)
    assert chat.keys == ["recommend.medical", "menu.body"] and groq.calls == 0


@pytest.mark.parametrize(
    "text",
    [
        "lose my body weight",
        "I want to lose a lot of my weight",
        "weight loss",
        "slim",
        "diet for fat loss",
        "वजन कम करना है",
        "मोटापा",
        "బరువు తగ్గాలి",
        "weight kam karna hai",
        "wajan kam",
        "baruvu taggali",
        "slim avvali",
        "something for diabetes",
    ],
)
def test_medical_data_matches(text):
    assert medical_matches(text)


@pytest.mark.parametrize(
    "text", ["classic oats bowl", "protein bowl", "weight of the pack", "lose the cart", "gym"]
)
def test_medical_data_does_not_match_ordinary_text(text):
    assert not medical_matches(text)


# ---------------------------------------------------------------- cue priority / real names


def test_a_cue_with_a_real_product_name_still_finds_the_product(chat, groq):
    onboard(chat)
    chat.send("suggest ragi mix")
    assert groq.calls == 0  # rules keep priority: the message names a real product
    assert "recommend.intro" not in chat.keys and any("Ragi Mix" in str(m) for m in chat.last)


def test_a_cue_beats_a_weak_search_guess_when_no_real_name_is_present(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext(sort="price_asc"))
    chat.send("what'd you recommend for my evening snack")
    assert len(groq.extract_messages) == 1 and groq.classify_messages == []
    assert chat.last[0].key == "recommend.intro" or any(
        m.key == "recommend.intro" for m in chat.last
    )


def test_when_the_flow_has_no_answer_the_rules_guess_is_kept(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext(request_type="other"))
    groq.classify_queue.append(cls("unknown"))
    chat.send("what'd you recommend for my evening snack")
    assert groq.calls == 2
    assert chat.keys == ["search.none_neutral"]  # the rules' guess, answered without echoing text


def test_other_rules_results_keep_their_priority(chat, groq):
    onboard(chat)
    chat.send("add 2 makhana crunch")
    assert groq.calls == 0 and "cart.added" in chat.keys
    chat.send("suggest where is my order")
    assert groq.calls == 0


def test_new_cue_phrases_match():
    for text in (
        "what would you recommend",
        "What'd you recommend me",
        "recommend me something",
        "suggest me a snack",
    ):
        assert cue_matches(text)


# ---------------------------------------------------------------- never echo fragments


def test_no_match_reply_on_the_llm_path_never_repeats_customer_text(backend, store, clock, groq):
    chat = build(backend, store, clock, groq, recommend=False)  # LLM layer on, flow off
    onboard(chat)
    chat.send(PHONE_B)  # rules/fuzzy turn this into a garbled search
    assert chat.keys == ["search.none_neutral"]
    assert chat.last[0].body == "I couldn't find that on the menu. You can browse instead."
    assert "recommend" not in chat.last[0].body and '"' not in chat.last[0].body


def test_the_old_quoted_reply_is_unchanged_without_the_llm_layer(backend, store, clock, groq):
    chat = build(backend, store, clock, groq, with_llm=False)
    onboard(chat)
    chat.send("do you have zorkblat florp")
    assert chat.keys == ["search.none"] or chat.keys == ["fallback"]
    chat.send(PHONE_B)
    assert chat.keys == ["search.none"]
    # "weight" is never corrected into a quantity ("eight" -> 8)
    body = chat.last[0].body.lower()
    assert "lose weight" in body
    assert " eight" not in body


def test_neutral_reply_is_translated(backend, store, clock, groq):
    chat = build(backend, store, clock, groq, recommend=False)
    onboard(chat, lang="hi")
    chat.send(PHONE_B)
    assert chat.last[0].body == t("hi", "search.none_neutral")


# ---------------------------------------------------------------- goal -> sort


def test_gym_goal_sorts_by_protein_with_null_protein_last_and_says_so(chat, groq, backend):
    onboard(chat)
    groq.extract_queue.append(ext(category_slug="protein-active-nutrition"))  # no sort
    chat.send(PHONE_A)
    shown = names(chat)
    assert len(shown) == 3
    values = [protein_of(backend, n) for n in shown]
    assert values[:2] == sorted([v for v in values if v is not None], reverse=True)
    assert values[-1] is None  # the product without a protein figure is last
    intro = next(m for m in chat.last if m.key == "recommend.intro")
    assert intro.body.endswith("Sorted by protein, highest first.")
    assert intro.body.startswith("Here are 3 options from the Ivar menu:")


def test_whole_menu_gym_request_returns_the_highest_protein_products(chat, groq, backend):
    onboard(chat)
    groq.extract_queue.append(ext())  # no category, no sort
    chat.send(PHONE_A)
    top = sorted(
        (p for p in backend._products.values() if p.in_stock and p.protein_g is not None),
        key=lambda p: p.protein_g,
        reverse=True,
    )[:4]
    assert [protein_of(backend, n) for n in names(chat)] == [p.protein_g for p in top]
    assert max(p.protein_g for p in top) > 20  # real high-protein products, not oats bowls


def test_an_explicit_sort_from_the_model_is_not_overridden(chat, groq, backend):
    onboard(chat)
    groq.extract_queue.append(ext(sort="calories_asc"))
    chat.send(PHONE_A)
    calories = {p.name: p.calories_kcal for p in backend._products.values()}
    shown = [calories[n] for n in names(chat)]
    assert shown == sorted(shown)  # ordered by calories, as the model asked
    assert not any(m.key == "recommend.intro" and "protein" in m.body for m in chat.last)


def test_protein_with_a_category_stays_inside_that_category(chat, groq, backend):
    onboard(chat)
    groq.extract_queue.append(ext(category_slug="nuts-seeds-snacks"))
    chat.send("suggest high protein snacks")
    by_name = {p.name: p for p in backend._products.values()}
    shown = names(chat)
    assert shown and all(by_name[n].category_id == "nuts-seeds-snacks" for n in shown)
    values = [by_name[n].protein_g for n in shown]
    assert values == sorted(values, reverse=True)


def test_recommendations_never_change_the_cart_here_either(chat, groq):
    onboard(chat)
    groq.extract_queue.append(ext())
    chat.send(PHONE_A)
    chat.send(PHONE_B)
    assert cart_of(chat) == {}


def test_the_goal_table_only_ever_sorts_by_protein():
    assert set(GOAL_SORT) == {"protein_desc"}  # nothing maps to calories or weight loss
    for text in ("lose weight", "low calorie", "weight loss", "slim", "diet"):
        assert goal_sort(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "good meals for the gym",
        "something for my workout",
        "post workout snack",
        "muscle gain food",
        "bulking options",
        "high protein please",
        "जिम के लिए कुछ",
        "वर्कआउट के बाद",
        "जिम",
        "జిమ్ కోసం",
        "వర్కౌట్ తర్వాత",
        "gym ke liye kuch",
        "gym kosam emi tinali",
    ],
)
def test_goal_words_in_each_language_map_to_a_protein_sort(text):
    assert goal_sort(text) == "protein_desc"


def test_goal_sort_never_overrides_and_only_applies_to_recommendations():
    base = RecommendRequest(kind="recommend")
    assert apply_goal_sort(base, "gym").sort == "protein_desc"
    assert apply_goal_sort(base, "gym").sort_from_goal is True
    assert (
        apply_goal_sort(RecommendRequest(kind="recommend", sort="price_asc"), "gym").sort
        == "price_asc"
    )
    assert apply_goal_sort(RecommendRequest(kind="medical"), "gym").sort is None
    assert apply_goal_sort(RecommendRequest(kind="product_question"), "gym").sort is None
    assert apply_goal_sort(base, "hello there").sort is None


def test_the_sorted_line_exists_in_all_three_languages():
    assert t("en", "recommend.sorted_protein") == "Sorted by protein, highest first."
    assert t("te", "recommend.sorted_protein") != t("en", "recommend.sorted_protein")
    assert t("hi", "recommend.sorted_protein") != t("en", "recommend.sorted_protein")


# ---------------------------------------------------------------- flags off


def test_flags_off_keeps_the_previous_behaviour(backend, store, clock, groq):
    chat = build(backend, store, clock, groq, recommend=False)
    onboard(chat)
    groq.classify_queue.append(cls("unknown"))
    chat.send(PHONE_A)
    assert chat.keys == ["fallback"] and groq.extract_messages == []  # classifier only
    chat.send(PHONE_B)  # no medical shortcut, no cue priority: the old rules path
    assert "recommend.medical" not in chat.keys


# ---------------------------------------------------------------- logging


def test_one_info_line_per_message_with_the_stage_and_no_customer_text(chat, groq, caplog):
    onboard(chat)
    groq.extract_queue.append(ext(category_slug="protein-active-nutrition"))
    with caplog.at_level(logging.INFO):
        chat.send(PHONE_A)
        chat.send(PHONE_B)
    lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("engine: recommend")
    ]
    assert len(lines) == 2
    assert "stage=cue kind=recommend constraints=['category_slug', 'sort']" in lines[0]
    assert "sort=protein_desc sort_from_goal=True outcome=picks picks=3" in lines[0]
    assert "stage=cue_medical kind=medical" in lines[1] and "outcome=medical" in lines[1]
    for secret in ("tonight", "lose my body", "meals for the"):
        assert secret not in caplog.text
