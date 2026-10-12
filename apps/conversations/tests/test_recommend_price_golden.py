"""The constraint golden set (recommend_price): shape, the dry-run command and the real decision
code. Dry run only: no network, no key."""

import json

import pytest
from django.core.management import call_command

from apps.conversations.intent_eval import (
    RECOMMEND_PRICE_GOLDEN,
    SET_NAMES,
    Harness,
    canned_reply,
    load_entries,
    load_recommend_golden,
)

ENTRIES = load_entries("recommend_price")


def test_the_set_is_registered_and_has_every_language():
    assert "recommend_price" in SET_NAMES and RECOMMEND_PRICE_GOLDEN.exists()
    assert 15 <= len(ENTRIES) <= 30
    assert {e["language"] for e in ENTRIES} == {"en", "hi", "te", "hinglish", "tenglish"}
    assert all(e["needs_native_review"] for e in ENTRIES if e["language"] != "en")
    assert len({e["id"] for e in ENTRIES}) == len(ENTRIES)
    assert all(e["max_price"] is not None for e in ENTRIES)


def test_the_other_sets_were_left_alone():
    assert len(load_recommend_golden()) == 50


def test_every_entry_takes_the_constraint_route_and_respects_its_limit():
    harness = Harness()
    for e in ENTRIES:
        record = harness.evaluate(e, "recommend_price", canned_reply(e, "recommend_price"), None)
        assert record["route"] == "constraint", e["id"]
        assert record["safety"] == [] and record["scores"]["price_ok"], e["id"]
        # nothing on the menu at or under a very low limit is the existing no-match reply
        assert record["reply_keys"][0] in ("recommend.intro", "recommend.none"), e["id"]
        if record["reply_keys"][0] == "recommend.none":
            assert e["max_price"] <= 20, e["id"]
        assert not record["failed"], e["id"]


def test_a_model_that_ignores_the_price_still_cannot_show_an_item_above_it():
    harness = Harness()
    e = next(e for e in ENTRIES if e["text"] == "Something high protein under 300")
    blank = json.loads(canned_reply(e, "recommend_price")) | {"max_price": None, "sort": None}
    record = harness.evaluate(e, "recommend_price", json.dumps(blank), None)
    assert record["scores"]["price_ok"] and record["reply_keys"][0] == "recommend.intro"


def test_the_command_option_runs_dry(settings, tmp_path):
    settings.IVA_DEV_RESET_ALLOWED = True
    out = tmp_path / "results.json"
    call_command("eval_intent_llm", golden_set="recommend_price", dry_run=True, output=str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["set"] == "recommend_price" and data["dry_run"] is True
    assert len(data["records"]) == len(ENTRIES) and not data["summary"]["safety"]["count"]


@pytest.mark.parametrize("flag", ["recommend_price"])
def test_the_command_still_refuses_outside_development(settings, flag):
    settings.IVA_DEV_RESET_ALLOWED = False
    with pytest.raises(Exception, match="development settings"):
        call_command("eval_intent_llm", golden_set=flag, dry_run=True)
