import json

import pytest

from apps.ai.intents import CLASSIFIABLE_INTENTS, TEXT_ENTITY_FIELDS, Intent
from apps.ai.validation import LLMOutputError, parse_llm_output

ENTITIES = {name: None for name in TEXT_ENTITY_FIELDS}


def output(**overrides):
    data = {"intent": "ADD_TO_CART", "confidence": 0.9, "entities": dict(ENTITIES)}
    entity_overrides = overrides.pop("entities", {})
    data.update(overrides)
    data["entities"].update(entity_overrides)
    return data


@pytest.mark.parametrize("intent", CLASSIFIABLE_INTENTS, ids=lambda i: i.value)
def test_every_classifiable_intent_is_accepted(intent):
    parsed = parse_llm_output(json.dumps(output(intent=intent.value)))
    assert parsed.intent is intent


def test_valid_output_with_entities():
    raw = json.dumps(
        output(
            entities={
                "product_query": " makhana ",
                "quantity": 2,
                "unit": "packet",
                "language_choice": "te",
                "order_number": "iva1002",
                "choice_index": 1,
            }
        )
    )
    parsed = parse_llm_output(raw)
    assert parsed.intent is Intent.ADD_TO_CART
    assert parsed.confidence == 0.9
    assert parsed.entities.product_query == "makhana"
    assert parsed.entities.quantity == 2
    assert parsed.entities.order_number == "IVA1002"
    assert parsed.entities.reference_id is None


def test_no_maximum_quantity_in_validation():
    parsed = parse_llm_output(json.dumps(output(entities={"quantity": 500})))
    assert parsed.entities.quantity == 500


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", "not json", "{", "[]", '"text"', "null",
        '{"intent": "HELP", "intent": "HELP", "confidence": 1, "entities": {}}',
        '{"intent": "HELP", "confidence": NaN, "entities": {}}',
        "x" * 5000,
    ],
)  # fmt: skip
def test_malformed_output_is_rejected(raw):
    with pytest.raises(LLMOutputError):
        parse_llm_output(raw)


def _without(key):
    data = output()
    del data[key]
    return data


def _without_entity(key):
    data = output()
    del data["entities"][key]
    return data


@pytest.mark.parametrize(
    "data",
    [
        # unknown intents
        output(intent="BUY_STOCK"),
        output(intent="CLARIFY"),
        output(intent="add_to_cart"),
        output(intent=3),
        # wrong types / ranges
        output(confidence="high"),
        output(confidence=True),
        output(confidence=1.5),
        output(confidence=-0.1),
        output(entities={"quantity": "2"}),
        output(entities={"quantity": 2.5}),
        output(entities={"quantity": True}),
        output(entities={"product_query": 7}),
        output(entities={"product_query": ""}),
        output(entities={"product_query": "x" * 101}),
        output(entities={"product_query": "bad\u0000text"}),
        output(entities={"unit": "kg"}),
        output(entities={"language_choice": "fr"}),
        output(entities={"choice_index": 0}),
        output(entities={"order_number": "IVA 1002; DROP"}),
        {"intent": "HELP", "confidence": 0.9, "entities": []},
        # missing fields
        _without("intent"),
        _without("confidence"),
        _without("entities"),
        _without_entity("quantity"),
        # extra fields
        output(price=199),
        output(entities={"product_id": "p1"}),
        output(entities={"reference_id": "p1"}),
        output(entities={"in_stock": True}),
    ],
)
def test_invalid_schema_is_rejected(data):
    with pytest.raises(LLMOutputError) as info:
        parse_llm_output(json.dumps(data))
    assert info.value.kind == "invalid_schema"


def test_error_message_never_echoes_the_output():
    secret = "919876541234"
    with pytest.raises(LLMOutputError) as info:
        parse_llm_output(json.dumps(output(intent=secret)))
    assert secret not in str(info.value)
