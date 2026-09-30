import string

import pytest

from apps.conversations.messages import CATALOGS, NEEDS_NATIVE_REVIEW, en, hi, t, te


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@pytest.mark.parametrize("module", [te, hi], ids=["te", "hi"])
def test_catalogs_have_the_same_keys_and_placeholders(module):
    assert set(module.MESSAGES) == set(en.MESSAGES)
    for key, text in en.MESSAGES.items():
        assert placeholders(module.MESSAGES[key]) == placeholders(text), key
    assert set(module.DIET_WORDS) == set(en.DIET_WORDS)


def test_telugu_and_hindi_are_flagged_for_review():
    assert set(NEEDS_NATIVE_REVIEW) == {"te", "hi"}
    for module in (te, hi):
        assert "NEEDS NATIVE-SPEAKER REVIEW" in module.__doc__


def test_translations_are_in_their_script():
    for key in ("fallback", "cart.empty", "help", "order.eta_na"):
        assert any("ఀ" <= ch <= "౿" for ch in CATALOGS["te"][key])
        assert any("ऀ" <= ch <= "ॿ" for ch in CATALOGS["hi"][key])


def test_unknown_language_falls_back_to_english():
    assert t("fr", "fallback") == en.MESSAGES["fallback"]
    assert t(None, "fallback") == en.MESSAGES["fallback"]


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        t("en", "no.such.key")
