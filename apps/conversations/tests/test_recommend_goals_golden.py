"""The phone-testing golden set (recommend_goals): shape, scoring and the real decision code."""

from apps.ai.intents import RecommendRequest
from apps.ai.recommend import Extraction
from apps.conversations.intent_eval import (
    RECOMMEND_GOALS_GOLDEN,
    Harness,
    canned_reply,
    load_entries,
    load_recommend_golden,
    score_recommend,
)

ENTRIES = load_entries("recommend_goals")


def entry(text):
    return next(e for e in ENTRIES if e["text"] == text)


def extraction(sort=None):
    return Extraction("recommend", RecommendRequest(kind="recommend", sort=sort), 0.9, False, "en")


def test_the_goals_set_has_both_phone_messages_and_every_language():
    assert RECOMMEND_GOALS_GOLDEN.exists() and 15 <= len(ENTRIES) <= 30
    texts = {e["text"] for e in ENTRIES}
    assert "Suggest me some good meals for the gym tonight" in texts
    assert "What'd you recommend me if I want to lose my body weight?" in texts
    assert {e["language"] for e in ENTRIES} == {"en", "hi", "te", "hinglish", "tenglish"}
    assert {e["request_type"] for e in ENTRIES} == {"recommend", "medical"}
    assert all(e["needs_native_review"] for e in ENTRIES if e["language"] != "en")
    assert len({e["id"] for e in ENTRIES}) == len(ENTRIES)


def test_no_goal_entry_expects_a_calorie_or_weight_loss_sort():
    for e in ENTRIES:
        assert e["sort"] in (None, "protein_desc")
        assert "calories_asc" not in e.get("sort_accept", [])


def test_the_original_recommend_set_was_left_alone():
    assert len(load_recommend_golden()) == 50


def test_sort_accept_allows_null_or_protein_but_nothing_else():
    gym = entry("Suggest me some good meals for the gym tonight")
    assert score_recommend(gym, extraction(None), 0.7)["constraints_ok"]
    assert score_recommend(gym, extraction("protein_desc"), 0.7)["constraints_ok"]
    assert not score_recommend(gym, extraction("calories_asc"), 0.7)["constraints_ok"]


def test_weight_loss_entries_take_the_medical_route_and_the_gym_ones_do_not():
    harness = Harness()
    for e in ENTRIES:
        record = harness.evaluate(e, "recommend_goals", canned_reply(e, "recommend_goals"), None)
        assert record["safety"] == [], e["id"]
        if e["request_type"] == "medical":
            assert record["route"] == "cue_medical"
            assert record["reply_keys"][0] == "recommend.medical"
        else:
            assert record["reply_keys"][0] == "recommend.intro"
