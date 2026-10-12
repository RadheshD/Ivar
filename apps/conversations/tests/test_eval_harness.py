"""eval_intent_llm: encoding, per-entry records, safety section, subset runs, dry-run.

Every Groq call is a mocked HTTP transport; nothing here reaches the network.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai.classifier import redact
from apps.ai.groq import GroqClassifier
from apps.conversations import intent_eval
from apps.conversations.channel import Button, OutButtons
from apps.conversations.engine import ConversationEngine
from apps.conversations.intent_eval import (
    CART_CONFIRMATION_PROMPT,
    CLARIFY,
    NO_ACTION,
    PLANNER_HANDOFF,
    READ_ONLY_REPLY,
    Harness,
    canned_reply,
    console_safe,
    load_golden,
    load_recommend_golden,
)
from apps.conversations.management.commands import eval_intent_llm
from apps.conversations.recommend_select import Selection
from apps.iva_common.http_transport import HttpResponse

GOLDEN = {"intents": load_golden(), "recommend": load_recommend_golden()}


def entry_by(set_name, **match):
    return next(e for e in GOLDEN[set_name] if all(e.get(k) == v for k, v in match.items()))


class MockedGroq:
    """A transport that answers like Groq: the ideal reply, unless overridden by entry id."""

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.requests = 0

    def request(self, method, url, *, headers=None, body=None, connect_timeout, read_timeout):
        self.requests += 1
        data = json.loads(body)
        schema = data["response_format"]["json_schema"]["name"]
        set_name = "recommend" if schema == "iva_request" else "intents"
        said = json.loads(data["messages"][1]["content"])["customer_message"]
        entry = next(e for e in GOLDEN[set_name] if redact(e["text"]) == said)
        text = self.overrides.get(entry["id"], canned_reply(entry, set_name))
        content = {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 10}}
        return HttpResponse(200, json.dumps(content).encode())


@pytest.fixture
def dev(settings):
    settings.IVA_DEV_RESET_ALLOWED = True
    settings.GROQ_API_KEY = "gsk_unit_test_key_not_real"
    return settings


@pytest.fixture
def mocked(monkeypatch):
    def install(overrides=None):
        transport = MockedGroq(overrides)
        monkeypatch.setattr(
            eval_intent_llm,
            "build_classifier",
            lambda key, model: GroqClassifier(key, model, transport=transport),
        )
        return transport

    return install


_R1 = json.loads(canned_reply(GOLDEN["recommend"][0], "recommend"))
WRONG_RECOMMEND = json.dumps({**_R1, "request_type": "other"})


def intent_reply(intent, slug=None, quantity=1, confidence=0.95):
    items = [{"product_slug": slug, "mention": "x", "quantity": quantity}] if slug else []
    return json.dumps(
        {"intent": intent, "confidence": confidence, "items": items, "language": "en",
         "ambiguous": False}
    )  # fmt: skip


def run(out_path, *args, stdout=None, **kwargs):
    stream = stdout or io.StringIO()
    kwargs.setdefault("sleep", 0)
    call_command("eval_intent_llm", *args, output=str(out_path), stdout=stream, **kwargs)
    return json.loads(out_path.read_text(encoding="utf-8")), stream


# ---------------------------------------------------------------- encoding


def test_console_safe_escapes_what_the_stream_cannot_encode():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    out = console_safe("కార్ట్ मेरा cart", stream)
    assert out.isascii() and "cart" in out and "\\u" in out
    assert console_safe("plain ascii", stream) == "plain ascii"


def test_cp1252_console_does_not_crash_and_the_json_keeps_the_original_text(dev, mocked, tmp_path):
    hindi = entry_by("intents", language="hi", intent="VIEW_CART")
    mocked({hindi["id"]: intent_reply("HELP")})  # wrong on purpose: it will be listed as a failure
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    data, _ = run(tmp_path / "r.json", ids=hindi["id"], stdout=stream)  # would raise before the fix
    printed = raw.getvalue().decode("cp1252")
    assert hindi["id"] in printed and "Failures" in printed  # the list after the table survived
    assert hindi["text"] not in printed and "\\u" in printed  # escaped on the console
    text = (tmp_path / "r.json").read_text(encoding="utf-8")
    assert hindi["text"] in text  # raw UTF-8, not \u escapes
    assert data["records"][0]["text"] == hindi["text"]


def test_telugu_survives_in_the_json_file(dev, mocked, tmp_path):
    telugu = entry_by("recommend", language="te", request_type="recommend")
    mocked()
    data, _ = run(tmp_path / "r.json", golden_set="recommend", ids=telugu["id"])
    assert data["records"][0]["text"] == telugu["text"]
    assert telugu["text"] in (tmp_path / "r.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- per-entry record


def test_intent_record_has_every_field_and_the_real_action_class(dev, mocked, tmp_path):
    entry = entry_by("intents", intent="ADD_TO_CART", language="en")
    slug = entry["items"][0]["slug"]
    mocked({
        entry["id"]: json.dumps({
            "intent": "ADD_TO_CART", "confidence": 0.9, "language": "en", "ambiguous": False,
            "items": [
                {"product_slug": slug, "mention": "m", "quantity": 99},
                {"product_slug": "ghost-slug", "mention": "g", "quantity": 1},
            ],
        })
    })  # fmt: skip
    data, _ = run(tmp_path / "r.json", ids=entry["id"])
    rec = data["records"][0]
    for key in (
        "id",
        "language",
        "expected",
        "raw",
        "validated",
        "outcome",
        "action_class",
        "reply_keys",
        "cart_changed",
        "scores",
        "safety",
        "failed",
        "shown_slugs",
    ):
        assert key in rec  # fmt: skip
    assert rec["raw"]["intent"] == "ADD_TO_CART" and len(rec["raw"]["items"]) == 2
    assert rec["validated"]["kept_slugs"] == [slug]
    assert rec["validated"]["dropped_slugs"] == ["ghost-slug"]
    assert rec["validated"]["quantity_nulled"] == [slug]  # 99 is above the port's maximum
    assert rec["action_class"] == CART_CONFIRMATION_PROMPT
    assert rec["reply_keys"] == ["llm.confirm_add"] and rec["cart_changed"] is False
    assert rec["safety"] == []  # the golden entry allows a cart change


def test_recommend_record_shows_dropped_constraints_and_a_read_only_reply(dev, mocked, tmp_path):
    entry = entry_by("recommend", text="something high-protein under ₹300")
    bad = json.loads(canned_reply(entry, "recommend"))
    bad.update(category_slug="not-a-category", max_price=300)
    mocked({entry["id"]: json.dumps(bad)})
    data, _ = run(tmp_path / "r.json", golden_set="recommend", ids=entry["id"])
    rec = data["records"][0]
    assert rec["validated"]["dropped_constraints"] == ["category_slug"]
    assert rec["raw"]["constraints"]["category_slug"] == "not-a-category"
    assert rec["action_class"] == READ_ONLY_REPLY and rec["shown_slugs"]
    assert rec["reply_keys"][0] == "recommend.intro" and rec["safety"] == []


def test_action_classes_for_the_other_flows():
    harness = Harness()
    plan = harness.evaluate(entry_by("recommend", request_type="diet_plan", language="en"),
                            "recommend", None, None)  # fmt: skip
    # a failed extraction call falls back to the category menu (a read-only reply)
    assert plan["action_class"] == READ_ONLY_REPLY and plan["reply_keys"] == ["menu.body"]
    cases = {
        "diet_plan": PLANNER_HANDOFF,
        "medical": READ_ONLY_REPLY,
        "other": NO_ACTION,
    }
    for request_type, expected in cases.items():
        e = entry_by("recommend", request_type=request_type, language="en")
        rec = harness.evaluate(e, "recommend", canned_reply(e, "recommend"), None)
        assert rec["action_class"] == expected, request_type
    amb = entry_by("recommend", ambiguous=True, language="en")
    assert (
        harness.evaluate(amb, "recommend", canned_reply(amb, "recommend"), None)["action_class"]
        == CLARIFY
    )


# ---------------------------------------------------------------- safety section


def test_a_unallowed_cart_offer_is_caught_and_exits_non_zero(dev, mocked, tmp_path):
    entry = entry_by("intents", intent="HELP", language="en")
    mocked({entry["id"]: intent_reply("ADD_TO_CART", "quinoa", 1)})
    out = tmp_path / "r.json"
    stream = io.StringIO()
    with pytest.raises(CommandError, match="Safety check failed"):
        call_command("eval_intent_llm", ids=entry["id"], output=str(out), stdout=stream)
    data = json.loads(out.read_text(encoding="utf-8"))  # results are still saved
    assert data["records"][0]["safety"] == ["a_unallowed_cart_offer"]
    assert (
        data["summary"]["safety"]["count"] == 1 and data["summary"]["safety"]["by_group"]["a"] == 1
    )
    assert f"UNSAFE {entry['id']}" in stream.getvalue()


def test_b_recommend_flow_must_never_offer_or_make_a_cart_change(dev, monkeypatch, tmp_path):
    entry = entry_by("recommend", text="something high-protein under ₹300")
    harness = Harness()
    prompt = [OutButtons("Add?", (Button("ACT:llmyes", "Yes"),), "llm.confirm_add")]
    monkeypatch.setattr(ConversationEngine, "_recommendation", lambda self, c, req: prompt)
    rec = harness.evaluate(entry, "recommend", canned_reply(entry, "recommend"), None)
    assert rec["action_class"] == CART_CONFIRMATION_PROMPT
    assert "b_recommend_cart_confirmation" in rec["safety"]

    def sneaky(self, c, req):  # changes the cart behind the customer's back
        self.backend.add_item(c.customer.id, "quinoa--std", 1)
        return []

    monkeypatch.setattr(ConversationEngine, "_recommendation", sneaky)
    rec = harness.evaluate(entry, "recommend", canned_reply(entry, "recommend"), None)
    assert rec["cart_changed"] is True and "b_recommend_cart_changed" in rec["safety"]


def test_c_a_shown_product_that_is_missing_or_out_of_stock_is_caught(dev, monkeypatch):
    entry = entry_by("recommend", text="something high-protein under ₹300")
    harness = Harness()
    real = next(iter(harness.backend._products.values()))
    harness.backend.set_stock(real.id, False)  # the Port says it is sold out
    sold_out = replace(real, in_stock=True)  # ...but a buggy selection still shows it
    ghost = replace(real, id="ghost-product", name="Ghost")
    monkeypatch.setattr(
        "apps.conversations.recommend_flow.select_products",
        lambda *a, **k: Selection(picks=[sold_out, ghost], matched=2),
    )
    rec = harness.evaluate(entry, "recommend", canned_reply(entry, "recommend"), None)
    assert rec["shown_missing"] == ["ghost-product"]
    assert rec["shown_out_of_stock"] == [real.id]
    assert {"c_shown_slug_missing", "c_shown_slug_out_of_stock"} <= set(rec["safety"])
    assert intent_eval.safety_summary([rec])["by_group"]["c"] == 2


def test_clean_runs_have_zero_safety_findings_and_exit_zero(dev, mocked, tmp_path):
    mocked()
    data, stream = run(tmp_path / "r.json", limit=12)
    assert data["summary"]["safety"]["count"] == 0
    assert "SAFETY: 0 unsafe" in stream.getvalue()


# ---------------------------------------------------------------- subset runs


@pytest.mark.parametrize(
    ("set_name", "ids"), [("intents", "g005,g015"), ("recommend", "r003,r010")]
)
def test_ids_run_only_those_entries_in_order(dev, mocked, tmp_path, set_name, ids):
    mocked()
    data, _ = run(tmp_path / "r.json", golden_set=set_name, ids=ids)
    assert [r["id"] for r in data["records"]] == ids.split(",")


@pytest.mark.parametrize("set_name", ["intents", "recommend"])
def test_unknown_ids_are_rejected(dev, mocked, tmp_path, set_name):
    mocked()
    with pytest.raises(CommandError, match="Unknown golden id"):
        run(tmp_path / "r.json", golden_set=set_name, ids="nope999")


@pytest.mark.parametrize(
    ("set_name", "wrong_entry", "wrong_reply"),
    [
        ("intents", "g001", intent_reply("HELP")),
        ("recommend", "r001", WRONG_RECOMMEND),
    ],
)  # fmt: skip
def test_failures_only_reruns_just_the_failed_entries(
    dev, mocked, tmp_path, set_name, wrong_entry, wrong_reply
):
    transport = mocked({wrong_entry: wrong_reply})
    first, _ = run(
        tmp_path / "first.json",
        golden_set=set_name,
        ids=f"{wrong_entry},g002" if set_name == "intents" else f"{wrong_entry},r002",
    )
    failed = [r["id"] for r in first["records"] if r["failed"]]
    assert failed == [wrong_entry]
    transport.requests = 0
    second, _ = run(
        tmp_path / "second.json",
        failures_only=True,
        from_results=str(tmp_path / "first.json"),
    )
    assert second["set"] == set_name  # taken from the saved file
    assert [r["id"] for r in second["records"]] == [wrong_entry]
    assert transport.requests == 1  # one call, not a full run


def test_failures_only_needs_a_results_file_and_a_matching_set(dev, mocked, tmp_path):
    mocked()
    with pytest.raises(CommandError, match="--from-results"):
        run(tmp_path / "r.json", failures_only=True)
    run(tmp_path / "saved.json", ids="g001")
    with pytest.raises(CommandError, match="not 'recommend'"):
        run(tmp_path / "r.json", golden_set="recommend", from_results=str(tmp_path / "saved.json"))


def test_failures_only_with_nothing_failed_runs_nothing(dev, mocked, tmp_path):
    mocked()
    run(tmp_path / "saved.json", ids="g001,g002")
    data, _ = run(
        tmp_path / "again.json", failures_only=True, from_results=str(tmp_path / "saved.json")
    )
    assert data["records"] == []


# ---------------------------------------------------------------- dry run


def test_dry_run_makes_no_http_calls_and_needs_no_key(dev, monkeypatch, tmp_path):
    dev.GROQ_API_KEY = ""

    def boom(*a, **k):
        raise AssertionError("dry-run must not touch the network")

    monkeypatch.setattr(eval_intent_llm, "build_classifier", boom)
    monkeypatch.setattr("http.client.HTTPSConnection", boom)
    monkeypatch.setattr("http.client.HTTPConnection", boom)
    for set_name, expected in (("intents", len(GOLDEN["intents"])), ("recommend", 50)):
        data, _ = run(tmp_path / f"{set_name}.json", golden_set=set_name, dry_run=True)
        assert len(data["records"]) == expected and data["dry_run"] is True
        assert data["model"] == "dry-run" and data["summary"]["safety"]["count"] == 0
        assert not any(r["api_error"] for r in data["records"])


def test_dry_run_still_refuses_outside_dev_mode(settings):
    settings.IVA_DEV_RESET_ALLOWED = False
    with pytest.raises(CommandError, match="development"):
        call_command("eval_intent_llm", dry_run=True)
