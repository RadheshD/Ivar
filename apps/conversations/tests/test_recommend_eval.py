"""The recommendation golden set and the eval scoring. Nothing here calls Groq."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai.intents import RecommendRequest
from apps.ai.recommend import SORT_KEYS, Extraction
from apps.conversations.management.commands.eval_intent_llm import (
    load_recommend_golden,
    score_recommend,
)


def entry(**over):
    base = {
        "id": "r1", "language": "en", "text": "x", "request_type": "recommend",
        "category_slug": None, "diet_type": None, "max_price": None, "min_price": None,
        "sort": None, "unsupported": None, "product_mention": None, "asked_fact": None,
        "ambiguous": False, "tags": [],
    }  # fmt: skip
    base.update(over)
    return base


def extraction(kind="recommend", confidence=0.9, ambiguous=False, **req):
    return Extraction(kind, RecommendRequest(kind=kind, **req), confidence, ambiguous, "en")


def test_golden_set_is_well_formed_and_covers_every_requested_style():
    entries = load_recommend_golden()
    assert 40 <= len(entries) <= 60
    assert len({e["id"] for e in entries}) == len(entries)
    assert {e["language"] for e in entries} == {"en", "hi", "te", "hinglish", "tenglish", "mixed"}
    types = {e["request_type"] for e in entries}
    assert types == {"recommend", "diet_plan", "product_question", "medical", "other"}
    tags = {t for e in entries for t in e["tags"]}
    assert {"medical", "allergy", "vegetarian", "price_only", "unsupported", "off_topic",
            "ambiguous", "injection", "phone_redaction"} <= tags  # fmt: skip
    assert all(e["needs_native_review"] for e in entries if e["language"] != "en")
    assert not any(e["needs_native_review"] for e in entries if e["language"] == "en")
    for e in entries:
        assert e["sort"] in (None, *SORT_KEYS)


def test_golden_categories_exist_in_the_catalog(backend):
    known = {c.id for g in backend.list_categories(None) for c in backend.list_categories(g.id)}
    known |= {g.id for g in backend.list_categories(None)}
    for e in load_recommend_golden():
        assert e["category_slug"] in (None, *known), e["id"]


def test_the_spec_examples_are_present_in_english():
    texts = {e["text"] for e in load_recommend_golden() if e["language"] == "en"}
    assert "something high-protein under ₹300" in texts
    assert "suggest a good breakfast for the gym" in texts
    assert "vegetarian snacks under 150" in texts
    assert "low calorie options" in texts


def test_score_exact_match():
    e = entry(sort="protein_desc", max_price=300)
    res = score_recommend(e, extraction(sort="protein_desc", max_price=Decimal("300")), 0.7)
    assert res["type_ok"] and res["constraints_ok"] and not res["wrong_handoff"]


def test_score_constraint_mismatch():
    e = entry(sort="protein_desc", max_price=300)
    res = score_recommend(e, extraction(sort="calories_asc", max_price=Decimal("300")), 0.7)
    assert res["type_ok"] and not res["constraints_ok"]


def test_wrong_handoff_and_unsafe_are_counted():
    medical = entry(request_type="medical")
    res = score_recommend(medical, extraction("recommend"), 0.7)
    assert res["wrong_handoff"] and res["unsafe"] and not res["type_ok"]
    other = entry(request_type="other")
    res = score_recommend(other, extraction("diet_plan"), 0.7)
    assert res["wrong_handoff"] and not res["unsafe"]
    assert not score_recommend(other, extraction("other"), 0.7)["wrong_handoff"]


def test_accepted_alternative_types_are_not_wrong():
    inj = entry(request_type="other", accept=["other", "medical"])
    assert not score_recommend(inj, extraction("medical"), 0.7)["wrong_handoff"]
    assert score_recommend(inj, extraction("medical"), 0.7)["type_ok"]


def test_a_failed_or_unsure_answer_is_not_a_hand_off():
    res = score_recommend(entry(request_type="medical"), None, 0.7)
    assert not res["wrong_handoff"] and not res["unsafe"] and not res["type_ok"]
    unsure = score_recommend(entry(ambiguous=True), extraction(confidence=0.3), 0.7)
    assert unsure["type_ok"] and unsure["constraints_ok"]


def test_eval_recommend_refuses_outside_dev_mode_and_without_a_key(settings):
    settings.IVA_DEV_RESET_ALLOWED = False
    settings.GROQ_API_KEY = "gsk_never_used"
    with pytest.raises(CommandError, match="development"):
        call_command("eval_intent_llm", golden_set="recommend")
    settings.IVA_DEV_RESET_ALLOWED = True
    settings.GROQ_API_KEY = ""
    with pytest.raises(CommandError, match="GROQ_API_KEY"):
        call_command("eval_intent_llm", golden_set="recommend")
