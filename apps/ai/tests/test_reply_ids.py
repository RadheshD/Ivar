import pytest

from apps.ai.intents import REPLY_INTENTS, Intent, Stage, build_reply_id, parse_reply_id
from apps.ai.llm import FakeLLMClient
from apps.ai.pipeline import IntentPipeline


@pytest.mark.parametrize("intent", REPLY_INTENTS, ids=lambda i: i.value)
def test_every_intent_has_a_reply_id(intent):
    llm = FakeLLMClient()
    result = IntentPipeline(llm).understand(reply_id=intent.value)
    assert result.intent is intent
    assert result.stage is Stage.BUTTON
    assert result.confidence == 1.0
    assert llm.calls == 0


@pytest.mark.parametrize(
    ("reply_id", "intent", "field", "value"),
    [
        ("LANGUAGE_SELECT:te", Intent.LANGUAGE_SELECT, "language_choice", "te"),
        ("ORDER_STATUS:IVA1002", Intent.ORDER_STATUS, "order_number", "IVA1002"),
        ("PAYMENT_STATUS:IVA1002", Intent.PAYMENT_STATUS, "order_number", "IVA1002"),
        ("TRACKING:IVA1002", Intent.TRACKING, "order_number", "IVA1002"),
        (
            "PRODUCT_SELECT:p_makhana-crunch",
            Intent.PRODUCT_SELECT,
            "reference_id",
            "p_makhana-crunch",
        ),
        ("CATEGORY_SELECT:cat_fresh", Intent.CATEGORY_SELECT, "reference_id", "cat_fresh"),
        ("REMOVE_FROM_CART:line.3", Intent.REMOVE_FROM_CART, "reference_id", "line.3"),
    ],
)
def test_reply_id_references(reply_id, intent, field, value):
    result = IntentPipeline().understand(reply_id=reply_id)
    assert result.intent is intent
    assert result.stage is Stage.BUTTON
    assert getattr(result.entities, field) == value


def test_reply_id_with_quantity():
    result = IntentPipeline().understand(reply_id="ADD_TO_CART:v_oats:3")
    assert result.intent is Intent.ADD_TO_CART
    assert result.entities.reference_id == "v_oats"
    assert result.entities.quantity == 3


@pytest.mark.parametrize(
    "reply_id",
    [
        "", "FALLBACK", "CLARIFY", "NOPE", "view_cart", "LANGUAGE_SELECT:fr",
        "ADD_TO_CART:bad ref", "ADD_TO_CART:v:x", "VIEW_CART:a:2", "ADD_TO_CART:a:1:2",
        "PRODUCT_SELECT:" + "x" * 65, "VIEW_CART" + ":" * 300, None, 42,
    ],
)  # fmt: skip
def test_invalid_reply_ids_are_rejected(reply_id):
    assert parse_reply_id(reply_id) is None


def test_invalid_reply_id_falls_back_to_the_text():
    result = IntentPipeline().understand("show my cart", reply_id="NOPE")
    assert result.intent is Intent.VIEW_CART
    assert result.stage is Stage.RULES


def test_invalid_reply_id_without_text_is_fallback():
    result = IntentPipeline().understand(reply_id="NOPE")
    assert result.intent is Intent.FALLBACK
    assert result.stage is Stage.NONE


def test_build_reply_id_round_trip():
    reply_id = build_reply_id(Intent.UPDATE_QUANTITY, "line_1", 4)
    assert reply_id == "UPDATE_QUANTITY:line_1:4"
    intent, entities = parse_reply_id(reply_id)
    assert intent is Intent.UPDATE_QUANTITY
    assert (entities.reference_id, entities.quantity) == ("line_1", 4)


@pytest.mark.parametrize(
    ("intent", "ref", "quantity"),
    [
        (Intent.FALLBACK, None, None),
        (Intent.CLARIFY, None, None),
        (Intent.VIEW_CART, "x", 2),
        (Intent.ADD_TO_CART, None, 2),
        (Intent.LANGUAGE_SELECT, "fr", None),
        (Intent.PRODUCT_SELECT, "has space", None),
    ],
)
def test_build_reply_id_rejects_bad_parts(intent, ref, quantity):
    with pytest.raises(ValueError):
        build_reply_id(intent, ref, quantity)
