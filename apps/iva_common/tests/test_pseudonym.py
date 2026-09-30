import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.iva_common.pseudonym import pseudonymize

KEY_A = "a" * 40
KEY_B = "b" * 40
WA = "919876500001"


def test_stable_and_not_the_number():
    with override_settings(IVA_PSEUDONYM_KEY=KEY_A):
        first, second = pseudonymize(WA), pseudonymize(WA)
    assert first == second
    assert len(first) == 64 and WA not in first


def test_independent_of_secret_key():
    with override_settings(IVA_PSEUDONYM_KEY=KEY_A, SECRET_KEY="one" * 20):
        first = pseudonymize(WA)
    with override_settings(IVA_PSEUDONYM_KEY=KEY_A, SECRET_KEY="two" * 20):
        second = pseudonymize(WA)
    assert first == second


def test_rotating_the_key_changes_every_pseudonym():
    with override_settings(IVA_PSEUDONYM_KEY=KEY_A):
        first = pseudonymize(WA)
    with override_settings(IVA_PSEUDONYM_KEY=KEY_B):
        second = pseudonymize(WA)
    assert first != second


@pytest.mark.parametrize("key", ["", None, "short"])
def test_missing_or_short_key_is_refused(key):
    with override_settings(IVA_PSEUDONYM_KEY=key), pytest.raises(ImproperlyConfigured):
        pseudonymize(WA)
