"""Transcripts: greetings and commands typed at the delivery-address step."""

import pytest

from .harness import ADDRESS, WA_ID, in_script, onboard


def at_address_step(chat, lang="en", name="Ravi", item="add 2 makhana"):
    onboard(chat, lang, name=name)
    chat.send(item)
    chat.send("checkout")
    assert chat.keys == ["checkout.type_address"], chat.keys
    return chat


def cart_of(chat):
    backend = chat.engine.backend
    customer = backend.get_or_create_customer(WA_ID)
    return {line.name: line.quantity for line in backend.get_cart(customer.id).lines}


def addresses_of(chat):
    backend = chat.engine.backend
    return backend.list_addresses(backend.get_or_create_customer(WA_ID).id)


# ------------------------------------------------------------------ greetings


@pytest.mark.parametrize(
    "greeting",
    ["Hi", "hi", "HELLO", "hello", "Hey", "  hey  ", "Hii", "hello!", "Hi.", "namaste", "Namaste",
     "namaskaram", "Namaskaram", "good morning", "hello there"],
)  # fmt: skip
def test_english_and_romanized_greetings_are_not_addresses(chat, greeting):
    at_address_step(chat)
    chat.send(greeting)
    assert chat.keys == ["checkout.welcome_back"]
    assert chat.body == "Welcome back, Ravi! You were in the middle of checkout."
    assert [b.title for b in chat.last[0].buttons] == ["Continue checkout", "Menu"]
    assert chat.ids == ["ACT:resume", "CATEGORY_SELECT"]
    assert addresses_of(chat) == []  # nothing was saved as an address
    assert cart_of(chat) == {"Makhana Crunch": 2}  # cart kept
    assert chat.session().state.name == "CHECKOUT_ADDRESS"  # checkout state kept


@pytest.mark.parametrize(
    ("lang", "greeting"),
    [("te", "నమస్తే"), ("te", "హలో"), ("te", "హాయ్"), ("te", "నమస్కారం"), ("hi", "नमस्ते"),
     ("hi", "हेलो"), ("hi", "हाय"), ("hi", "प्रणाम"), ("te", "namaskaram"), ("hi", "namaste")],
)  # fmt: skip
def test_telugu_and_hindi_greetings_get_a_welcome_back_in_the_customers_language(
    chat, lang, greeting
):
    at_address_step(chat, lang)
    chat.send(greeting)
    assert chat.keys == ["checkout.welcome_back"]
    assert "Ravi" in chat.body and in_script(chat.body, lang)
    assert in_script(chat.last[0].buttons[0].title, lang)
    assert len(chat.last[0].buttons[0].title) <= 20  # fits a WhatsApp button
    assert addresses_of(chat) == []


def test_a_greeting_works_for_a_customer_with_no_name(chat):
    at_address_step(chat, name=None)
    chat.send("hi")
    assert chat.body == "Welcome back! You were in the middle of checkout."


def test_a_greeting_then_an_address_still_works(chat):
    at_address_step(chat)
    chat.send("hi")
    chat.send(ADDRESS)
    assert chat.keys == ["checkout.summary", "checkout.confirm"]


# ------------------------------------------------------------------ Continue checkout


def test_continue_checkout_returns_to_the_address_prompt_with_the_cart_intact(chat):
    at_address_step(chat)
    chat.send("hello")
    chat.tap("ACT:resume")
    assert chat.keys == ["checkout.type_address"]
    assert cart_of(chat) == {"Makhana Crunch": 2}
    assert chat.session().state.name == "CHECKOUT_ADDRESS"
    chat.send(ADDRESS)
    assert chat.keys == ["checkout.summary", "checkout.confirm"]
    assert "Makhana Crunch" in chat.body


def test_continue_checkout_offers_saved_addresses_when_there_are_some(chat):
    at_address_step(chat)
    chat.send(ADDRESS)  # saved as an address, checkout left unconfirmed
    chat.send("checkout")
    assert chat.keys == ["checkout.ask_address"]
    chat.send("hello")
    chat.tap("ACT:resume")
    assert chat.keys == ["checkout.ask_address"]
    assert cart_of(chat) == {"Makhana Crunch": 2}


def test_continue_checkout_with_an_emptied_cart_says_so(chat):
    at_address_step(chat)
    chat.send("hello")
    backend = chat.engine.backend
    customer = backend.get_or_create_customer(WA_ID)
    for line in backend.get_cart(customer.id).lines:
        backend.remove_item(customer.id, line.line_id)
    chat.tap("ACT:resume")
    assert "checkout.type_address" not in chat.keys and chat.keys == ["cart.empty"]


def test_menu_button_on_the_welcome_back_message_goes_to_the_menu(chat):
    at_address_step(chat)
    chat.send("hi")
    chat.tap("CATEGORY_SELECT")
    assert chat.keys == ["menu.body"]
    assert cart_of(chat) == {"Makhana Crunch": 2}


# ------------------------------------------------------------------ commands


@pytest.mark.parametrize("word", ["menu", "Menu", "cancel", "CANCEL", "stop", "back"])
def test_menu_and_cancel_leave_checkout_for_the_menu(chat, word):
    at_address_step(chat)
    chat.send(word)
    assert chat.keys == ["menu.body"]
    assert addresses_of(chat) == []


@pytest.mark.parametrize("word", ["cart", "my cart", "My Cart", "view cart", "show my cart"])
def test_cart_commands_show_the_cart(chat, word):
    at_address_step(chat)
    chat.send(word)
    assert chat.keys[0] == "cart.title"
    assert "Makhana Crunch" in chat.body
    assert addresses_of(chat) == []


@pytest.mark.parametrize(
    ("lang", "word"),
    [("te", "నా కార్ట్"), ("te", "కార్ట్ చూపించు"), ("hi", "मेरा कार्ट"), ("hi", "mera cart dikhao")],
)
def test_cart_commands_in_telugu_and_hindi(chat, lang, word):
    at_address_step(chat, lang)
    chat.send(word)
    assert chat.keys[0] == "cart.title"
    assert addresses_of(chat) == []


def test_help_and_order_status_and_language_commands(chat):
    at_address_step(chat)
    chat.send("help")
    assert chat.keys == ["help"]
    chat.send("checkout")  # back to the address step for the next command
    chat.send("order status")
    assert chat.keys[0].startswith("order.")
    chat.send("checkout")
    chat.send("change language")
    assert chat.ids == ["LANGUAGE_SELECT:en", "LANGUAGE_SELECT:te", "LANGUAGE_SELECT:hi"]
    assert addresses_of(chat) == []


# ------------------------------------------------------------------ real addresses still work


@pytest.mark.parametrize(
    "address",
    [
        "Cart Road, Kondapur, Hyderabad",
        "Menu Street 5, Madhapur, Hyderabad 500081",
        "Help Nagar, Flat 3, Hyderabad",
        "Order House, Plot 4, Banjara Hills, Hyderabad",
        "12, Cancel Colony, Gachibowli",
        "Hi-Tech City Road, Madhapur, Hyderabad",
        "hello apartments, Kukatpally, Hyderabad",
        "Back Lane, Near Metro, Ameerpet",
        "Namaste Residency, Flat 301, Jubilee Hills",
        "my cart road 22, Kondapur",
        "Hi, Flat 12, Green Park Road, Madhapur",
    ],
)
def test_addresses_containing_command_words_are_accepted(chat, address):
    at_address_step(chat)
    chat.send(address)
    assert chat.keys == ["checkout.summary", "checkout.confirm"], chat.keys
    assert [a.text for a in addresses_of(chat)] == [" ".join(address.split())]


def test_short_junk_that_is_not_a_command_is_still_rejected_as_an_address(chat):
    at_address_step(chat)
    chat.send("ok thanks")
    assert chat.keys == ["checkout.address_invalid"]


def test_a_greeting_outside_the_address_step_is_unchanged(chat):
    onboard(chat)
    chat.send("hi")
    assert chat.keys[0] == "greeting"
