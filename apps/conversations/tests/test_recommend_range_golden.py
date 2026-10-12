"""The range golden set (recommend_range): shape, the dry-run command and the real decision
code. Dry run only: no network, no key."""

import json
from decimal import Decimal

from django.core.management import call_command

from apps.ai import constraints
from apps.ai.text import tokenize
from apps.conversations.intent_eval import (
    RECOMMEND_RANGE_GOLDEN,
    SET_NAMES,
    Harness,
    canned_reply,
    load_entries,
)

ENTRIES = load_entries("recommend_range")


def test_the_set_is_registered_and_has_every_language():
    assert "recommend_range" in SET_NAMES and RECOMMEND_RANGE_GOLDEN.exists()
    assert 20 <= len(ENTRIES) <= 30
    assert {e["language"] for e in ENTRIES} == {"en", "hi", "te", "hinglish", "tenglish"}
    assert all(e["needs_native_review"] for e in ENTRIES if e["language"] != "en")
    assert len({e["id"] for e in ENTRIES}) == len(ENTRIES)


def test_every_entry_takes_the_constraint_route_and_stays_inside_its_bounds():
    harness = Harness()
    for e in ENTRIES:
        record = harness.evaluate(e, "recommend_range", canned_reply(e, "recommend_range"), None)
        assert record["route"] == "constraint", e["id"]
        assert record["safety"] == [] and record["scores"]["price_ok"], e["id"]
        assert record["reply_keys"][0] in ("recommend.intro", "recommend.none"), e["id"]
        assert not record["failed"], e["id"]


def test_a_model_that_ignores_the_range_still_cannot_show_an_item_outside_it():
    harness = Harness()
    e = next(e for e in ENTRIES if e["text"] == "High protein 250-300")
    blank = json.loads(canned_reply(e, "recommend_range")) | {
        "min_price": None,
        "max_price": None,
        "sort": None,
    }
    record = harness.evaluate(e, "recommend_range", json.dumps(blank), None)
    assert record["scores"]["price_ok"] and record["reply_keys"][0] == "recommend.intro"
    assert any(k == "recommend.card" for k in record["reply_keys"])


def test_the_command_option_runs_dry(settings, tmp_path):
    settings.IVA_DEV_RESET_ALLOWED = True
    out = tmp_path / "results.json"
    call_command("eval_intent_llm", golden_set="recommend_range", dry_run=True, output=str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["set"] == "recommend_range" and data["dry_run"] is True
    assert len(data["records"]) == len(ENTRIES) and not data["summary"]["safety"]["count"]


def test_every_entry_parses_to_its_expected_bounds_and_diet():
    for e in ENTRIES:
        text = e["text"]
        ranged = constraints.parse(tokenize(constraints.prepare_ranges(text)))
        c = ranged if ranged.has_range else constraints.parse(tokenize(constraints.prepare(text)))
        want_min = None if e["min_price"] is None else Decimal(e["min_price"])
        want_max = None if e["max_price"] is None else Decimal(e["max_price"])
        assert (c.min_price, c.max_price) == (want_min, want_max), text
        assert c.diet_type == e["diet_type"], text
