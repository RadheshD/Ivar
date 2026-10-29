"""Customer free text never reaches the logs.

Only masked IDs, state names, intents and error types may be logged. This runs a whole
conversation, including failure paths, with every logger captured at DEBUG.
"""

import logging

from apps.ai.llm import FakeLLMClient, LLMError
from apps.ai.pipeline import IntentPipeline
from apps.conversations.backend_port import BackendUnavailable

from .harness import ADDRESS, WA_ID, Chat, checkout_to_payment, onboard

NAME = "Ravi Kumar"
CITY = "Secunderabad"
PLAN_MESSAGE = (
    "IVA PLAN v1\n"
    "goal: healthy_eating\n"
    "diet: veg\n"
    "calories: 2446\n"
    "protein_g: 91\n"
    "bmi: 22.5\n"
    "bmr: 1500\n"
    "picks: Plant Protein Mix; Active Protein"
)
MESSAGES = (
    "do you have makhana",
    "add 2 packets makhana",
    "can we talk about the weather please",
    "remove the makhana",
    "my number is 919812345678 call me",
    PLAN_MESSAGE,
)


def _all_log_text(records) -> str:
    parts = []
    for record in records:
        parts.append(record.getMessage())
        if record.exc_text:
            parts.append(record.exc_text)
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
    return "\n".join(parts)


def test_free_text_is_never_logged(make_engine, flaky, caplog):
    llm = FakeLLMClient(
        [LLMError("down"), "not json", FakeLLMClient.reply("HELP", 0.2)],
        default=FakeLLMClient.reply("HELP", 0.9),
    )
    chat = Chat(make_engine(flaky, pipeline=IntentPipeline(llm)))
    with caplog.at_level(logging.DEBUG):
        onboard(chat, name=NAME, city=CITY)
        for text in MESSAGES:
            chat.send(text)
        checkout_to_payment(chat)  # types the address
        flaky.fail["get_cart"] = BackendUnavailable("down")
        chat.send("show my cart")
        flaky.fail["get_cart"] = ValueError(f"bad value {ADDRESS}")
        chat.send("show my cart")
        del flaky.fail["get_cart"]
        chat.send("where is my order")

    logged = _all_log_text(caplog.records)
    assert caplog.records, "expected the engine to log something"
    for private in (
        NAME, CITY, ADDRESS, WA_ID, "919812345678", "22.5", "1500",
        "Plant Protein Mix", "Active Protein", *MESSAGES,
    ):  # fmt: skip
        assert private not in logged, private


def test_contact_profile_name_is_never_logged(chat, caplog):
    with caplog.at_level(logging.DEBUG):
        chat.send("hi")
        chat.tap("LANGUAGE_SELECT:en", profile_name="Priya Sharma")
    logged = _all_log_text(caplog.records)
    assert "Priya Sharma" not in logged


def test_engine_logs_only_intent_stage_and_state(chat, caplog):
    onboard(chat)
    with caplog.at_level(logging.INFO, logger="apps.conversations.engine"):
        chat.send("do you have makhana")
    engine_lines = [r.getMessage() for r in caplog.records if r.name.endswith("engine")]
    assert engine_lines == ["engine: intent=PRODUCT_SEARCH stage=rules state=MAIN_MENU"]


def test_order_history_paths_never_log_name_address_or_wa_id(chat, caplog):
    with caplog.at_level(logging.DEBUG):
        onboard(chat, name=NAME, city=CITY)
        first = checkout_to_payment(chat)
        chat.send("my orders")  # one order: its detail
        backend = chat.engine.backend
        customer = backend.get_or_create_customer(WA_ID)
        backend.add_item(customer.id, "quinoa--std", 1)
        address = backend.list_addresses(customer.id)[0]
        backend.checkout(customer.id, address.id, "second-order")
        chat.send("my orders")  # two orders: the list
        chat.tap("ACT:orders")
        chat.tap(f"ORDER_STATUS:{first}")
        chat.send("where is my order")
        chat.send("order history")
    logged = _all_log_text(caplog.records)
    assert caplog.records, "expected the engine to log something"
    for private in (NAME, CITY, ADDRESS, "Green Park", WA_ID):
        assert private not in logged, private


def test_reorder_paths_never_log_name_address_or_wa_id(chat, caplog):
    with caplog.at_level(logging.DEBUG):
        onboard(chat, name=NAME, city=CITY)
        order = checkout_to_payment(chat)
        backend = chat.engine.backend
        chat.tap(f"ORDER_STATUS:{order}")
        chat.tap(f"ACT:reorder:{order}")  # one tap into an empty cart
        chat.tap(f"ACT:reorder:{order}")  # repeated tap
        chat.send("add 2 packets makhana")  # any cart change clears the "already applied" marker
        backend.set_price("makhana-crunch", 999)
        chat.tap(f"ACT:reorder:{order}")  # a cart with items: preview with three buttons
        assert chat.keys == ["reorder.preview"] and len(chat.last[0].buttons) == 3
        for button in [b for m in chat.last for b in getattr(m, "buttons", ())]:
            if button.id.startswith(("ACT:reorder_replace", "ACT:reorder_add")):
                chat.tap(button.id)
        chat.tap(f"ACT:reorder_cancel:{order}")
        chat.tap("ACT:reorder:IVA9999")  # an order that does not exist
    logged = _all_log_text(caplog.records)
    assert caplog.records, "expected the engine to log something"
    for private in (NAME, CITY, ADDRESS, "Green Park", WA_ID):
        assert private not in logged, private
