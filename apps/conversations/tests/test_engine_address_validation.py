"""Free text typed at "Where should we deliver?" must look like an address.

Nonsense ("zork florp", "yes", an emoji) is asked again, with an example of a good address; real
addresses, with or without a PIN code and in any supported language or script, are accepted.
"""

import pytest

from apps.conversations.messages import CATALOGS, t

from .harness import ADDRESS, onboard
from .test_engine_checkout_commands import addresses_of, at_address_step

ASKED_AGAIN = ("checkout.address_invalid", "checkout.address_unclear")
ACCEPTED_KEYS = ["checkout.summary", "checkout.confirm"]


@pytest.mark.parametrize(
    "junk",
    [
        "zork florp",  # ten characters, two words: passed the old length-only check
        "zork florp blah",
        "asdf",
        "ok",
        "yes",
        "no",
        "ok thanks",
        "no thanks please",
        "Hyderabad",  # a single word
        "asdfghjklqwerty",  # long, but a single word
        "yes yes yes yes",  # long, but one distinct word
        "😀",
        "😀😀😀😀😀😀😀😀😀😀😀😀",
        "😀 😀 😀 😀 😀 😀 😀",
        "a b",
        "12",
        "?!?!?!?!?!?!",
    ],
)
def test_nonsense_is_asked_again_and_never_stored(chat, junk):
    at_address_step(chat)
    chat.send(junk)
    assert chat.keys[0] in ASKED_AGAIN, chat.keys
    assert addresses_of(chat) == []  # nothing was saved
    assert chat.session().state.name == "CHECKOUT_ADDRESS"  # still waiting for an address
    chat.send(ADDRESS)  # a real address afterwards still works
    assert chat.keys == ACCEPTED_KEYS


@pytest.mark.parametrize("junk", ["zork florp", "zork florp blah", "asdfghjklqwerty"])
def test_long_enough_nonsense_gets_the_message_that_shows_a_good_address(chat, junk):
    at_address_step(chat)
    chat.send(junk)
    assert chat.keys == ["checkout.address_unclear"]
    assert chat.body == t("en", "checkout.address_unclear")
    assert "Flat 4B, Road 12, Banjara Hills, Hyderabad" in chat.body  # the example


@pytest.mark.parametrize(
    "address",
    [
        "Flat 4B, Road 12, Banjara Hills, Hyderabad 500034",
        "Flat 4B, Road 12, Banjara Hills, Hyderabad",  # no PIN code
        "Plot 5 Madhapur",  # three words with a number
        "Jubilee Hills Hyderabad",  # three words, an address word
        "Door 7-1-23 Ameerpet Hyderabad",
        "फ्लैट 4B, रोड 12, बंजारा हिल्स, हैदराबाद 500034",  # Hindi
        "मकान नंबर बंजारा हिल्स हैदराबाद",  # Hindi, no digits
        "ఫ్లాట్ 4B, రోడ్డు 12, బంజారా హిల్స్, హైదరాబాద్ 500034",  # Telugu
        "ఇంటి నంబర్ బంజారా హిల్స్ హైదరాబాద్",  # Telugu, no digits
        "Flat 4B, Road 12, Banjara Hills ke paas, Hyderabad",  # Hinglish
        "mera ghar Banjara Hills Hyderabad mein hai",  # Hinglish, no digits
        "naa illu Banjara Hills lo, Road 12, Hyderabad",  # Tenglish
        "maa inti dagara Banjara Hills Hyderabad",  # Tenglish, no digits
    ],
)
def test_real_addresses_are_accepted(chat, address):
    at_address_step(chat)
    chat.send(address)
    assert chat.keys == ACCEPTED_KEYS, chat.keys
    assert [a.text for a in addresses_of(chat)] == [" ".join(address.split())]


@pytest.mark.parametrize("greeting", ["Hi", "hello", "namaste"])
def test_a_greeting_during_address_entry_is_still_not_an_address(chat, greeting):
    at_address_step(chat)
    chat.send(greeting)
    assert chat.keys == ["checkout.welcome_back"]
    assert addresses_of(chat) == []


def test_saved_addresses_still_work(chat):
    at_address_step(chat)
    chat.send(ADDRESS)
    chat.tap("ACT:cancel")
    chat.send("add 1 makhana").send("checkout")
    assert chat.keys == ["checkout.ask_address"]
    chat.tap(chat.ids[0])  # the saved address
    assert chat.keys == ACCEPTED_KEYS
    assert len(addresses_of(chat)) == 1


def test_a_rejected_address_never_reaches_the_port_or_the_log(chat, caplog):
    at_address_step(chat)
    with caplog.at_level("DEBUG"):
        chat.send("zork florp")
    assert "zork" not in caplog.text and "zork" not in chat.body  # never repeated back


@pytest.mark.parametrize("lang", ["hi", "te"])
def test_the_reask_is_translated_and_shows_an_example(chat, lang):
    onboard(chat, lang)
    chat.send("add 2 makhana").send("checkout")
    chat.send("zork florp")
    assert chat.keys == ["checkout.address_unclear"]
    assert chat.body == t(lang, "checkout.address_unclear")
    assert chat.body != t("en", "checkout.address_unclear")
    assert "Flat 4B, Road 12, Banjara Hills, Hyderabad" in chat.body


def test_the_reask_exists_in_every_catalog():
    for lang in ("en", "hi", "te"):
        assert CATALOGS[lang]["checkout.address_unclear"].strip()
