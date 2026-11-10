"""Transcripts: "thanks" and "bye" in every conversation state."""

import pytest

from apps.conversations.backend_port import BackendUnavailable
from apps.conversations.messages import t

from .harness import ADDRESS, WA_ID, checkout_to_payment, in_script, onboard

THANKS_EN = "You're welcome, Ravi! 😊 Anything else I can help with?"
BYE_EN = "Bye, Ravi! 👋 Message me anytime you'd like to order."


def cart_of(chat):
    backend = chat.engine.backend
    customer = backend.get_or_create_customer(WA_ID)
    return {line.name: line.quantity for line in backend.get_cart(customer.id).lines}


def addresses_of(chat):
    backend = chat.engine.backend
    return backend.list_addresses(backend.get_or_create_customer(WA_ID).id)


def snapshot(chat):
    s = chat.session()
    return (s.state, s.pending, s.address_id, s.order_number, s.idempotency_key, cart_of(chat))


def button_titles(chat):
    return [b.title for b in chat.last[0].buttons]


# ------------------------------------------------------------------ idle


@pytest.mark.parametrize(
    ("text", "key", "body"),
    [
        ("Thanks", "closing.thanks", THANKS_EN),
        ("thank you", "closing.thanks", THANKS_EN),
        ("Bye", "closing.bye", BYE_EN),
    ],
)
def test_idle_replies_are_closing_not_greeting_or_fallback(chat, text, key, body):
    onboard(chat)
    chat.send(text)
    assert chat.keys == [key]
    assert chat.body == body
    assert button_titles(chat) == ["Menu"]  # empty cart: no View cart, no "cart saved" line


def test_greeting_is_unchanged(chat):
    onboard(chat)
    chat.send("Hi")
    assert chat.keys[0] == "greeting"
    assert chat.body.startswith("Hi Ravi! 👋")


def test_no_name_omits_it_gracefully(chat):
    onboard(chat, name=None)
    chat.send("thanks")
    assert chat.body == "You're welcome! 😊 Anything else I can help with?"
    chat.send("bye")
    assert chat.body == "Bye! 👋 Message me anytime you'd like to order."


# ------------------------------------------------------------------ cart


def test_with_items_in_the_cart_it_offers_view_cart_and_says_it_is_saved(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    assert cart_of(chat) == {"Makhana Crunch": 2}
    before = snapshot(chat)
    chat.send("thanks")
    assert chat.keys == ["closing.thanks"]
    assert chat.body == f"{THANKS_EN}\nYour cart is still saved."
    assert button_titles(chat) == ["Menu", "View cart"]
    assert chat.ids == ["CATEGORY_SELECT", "VIEW_CART"]
    assert snapshot(chat) == before  # cart and state untouched
    chat.send("bye")
    assert chat.body == f"{BYE_EN}\nYour cart is still saved."
    assert snapshot(chat) == before


def test_the_saved_cart_claim_is_true_it_outlives_the_session(chat, store):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("bye")
    assert "still saved" in chat.body
    store._data.clear()  # the session is gone (expired)
    assert cart_of(chat) == {"Makhana Crunch": 2}  # the cart is held by the Port, not the session
    chat.send("view cart")
    assert "cart.empty" not in chat.keys


def test_an_empty_cart_makes_no_saved_claim(chat):
    onboard(chat)
    chat.send("bye")
    assert "saved" not in chat.body


def test_cart_check_failure_still_replies(chat, flaky):
    onboard(chat)
    chat.send("add 2 makhana")
    flaky.fail["get_cart"] = BackendUnavailable("down")
    chat.send("thanks")
    assert chat.keys == ["closing.thanks"]
    assert button_titles(chat) == ["Menu"]


# ------------------------------------------------------------------ address collection


@pytest.mark.parametrize("text", ["thanks", "Thank you", "bye", "thanks bye", "ty", "dhanyavaad"])
def test_never_saved_as_an_address(chat, text):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    assert chat.keys == ["checkout.type_address"]
    before = snapshot(chat)
    chat.send(text)
    if text in ("bye", "thanks bye"):
        assert chat.keys == ["closing.bye"]  # a goodbye is not followed by a question
    else:
        assert chat.keys == ["closing.thanks", "checkout.type_address"]  # asks again
    assert addresses_of(chat) == []
    assert snapshot(chat) == before  # still at the address step, cart kept
    assert chat.session().state.name == "CHECKOUT_ADDRESS"
    chat.send(ADDRESS)  # the checkout carries on
    assert "checkout.confirm" in chat.keys


def test_an_address_that_contains_a_closing_word_is_still_an_address(chat):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    chat.send("Flat 12, Thanks Road, Kondapur, Hyderabad 500084")
    assert "checkout.confirm" in chat.keys
    assert len(addresses_of(chat)) == 1


# ------------------------------------------------------------------ confirmation


@pytest.mark.parametrize("text", ["thanks", "bye", "ok thanks", "thanks bye"])
def test_confirmation_is_not_confirmed_cancelled_or_edited(chat, text):
    onboard(chat)
    chat.send("add 2 makhana")
    chat.send("checkout")
    chat.send(ADDRESS)
    assert "checkout.confirm" in chat.keys
    before = snapshot(chat)
    chat.send(text)
    assert chat.keys[0] in ("closing.thanks", "closing.bye")
    assert snapshot(chat) == before
    assert chat.session().state.name == "CHECKOUT_CONFIRM"
    assert chat.session().order_number is None  # no order was placed
    chat.tap("ACT:confirm")  # the pending order can still be confirmed afterwards
    assert "checkout.created" in chat.keys


# ------------------------------------------------------------------ after the order


@pytest.mark.parametrize(
    ("text", "key"),
    [("Thanks", "closing.thanks"), ("thank you", "closing.thanks"), ("Bye", "closing.bye")],
)
def test_right_after_an_order_is_placed(chat, text, key):
    onboard(chat)
    number = checkout_to_payment(chat)
    before = snapshot(chat)
    chat.send(text)
    assert chat.keys == [key]  # not "Sorry, I didn't understand that."
    assert snapshot(chat) == before
    assert chat.session().order_number == number
    assert chat.session().state.name == "AWAITING_PAYMENT"
    assert button_titles(chat) == ["Menu"]  # the cart was emptied by the order


# ------------------------------------------------------------------ a pending question


def test_a_pending_question_survives(chat):
    onboard(chat)
    chat.send("add makhana")
    before = (chat.session().pending, chat.session().state, chat.session().listing)
    chat.send("thanks")
    assert chat.keys[0] == "closing.thanks"
    after = (chat.session().pending, chat.session().state, chat.session().listing)
    assert after == before


# ------------------------------------------------------------------ languages


@pytest.mark.parametrize(
    ("lang", "text", "kind"),
    [
        ("en", "dhanyavaad", "thanks"), ("en", "thanks", "thanks"), ("en", "bye", "bye"),
        ("hi", "धन्यवाद", "thanks"), ("hi", "अलविदा", "bye"),
        ("te", "ధన్యవాదాలు", "thanks"), ("te", "బై", "bye"),
    ],
)  # fmt: skip
def test_reply_is_in_the_saved_language_and_script(chat, lang, text, kind):
    onboard(chat, lang)
    chat.send(text)
    assert chat.keys == [f"closing.{kind}"]
    assert in_script(chat.body, lang), chat.body
    assert chat.body == t(lang, f"closing.{kind}", name="Ravi")


@pytest.mark.parametrize(
    ("lang", "text", "expected"),
    [
        ("hi", "dhanyavaad", "Aapka swagat hai, Ravi! 😊 Aur kuch help chahiye?"),
        ("hi", "shukriya", "Aapka swagat hai, Ravi! 😊 Aur kuch help chahiye?"),
        ("hi", "bye", "Bye, Ravi! 👋 Jab bhi order karna ho, mujhe message kar dena."),
        ("te", "dhanyavadalu", "Welcome, Ravi! 😊 Inkemaina help kavala?"),
        ("te", "thanks", "Welcome, Ravi! 😊 Inkemaina help kavala?"),
        ("te", "bye", "Bye, Ravi! 👋 Order cheyalanukunnappudu eppudaina naaku message cheyandi."),
    ],
)
def test_romanized_message_gets_the_hinglish_or_tenglish_reply(chat, lang, text, expected):
    onboard(chat, lang)
    chat.send(text)
    assert chat.body == expected


@pytest.mark.parametrize("lang", ["hi", "te"])
def test_cart_saved_line_follows_the_language(chat, lang):
    onboard(chat, lang)
    chat.send("add 2 makhana")
    assert cart_of(chat)  # the add worked
    chat.send("bye")  # Latin letters: the romanized line
    assert t(lang, "closing.cart_saved_roman") in chat.body
    chat.send("బై" if lang == "te" else "अलविदा")
    assert t(lang, "closing.cart_saved") in chat.body
