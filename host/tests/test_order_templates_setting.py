"""``IVA_ORDER_STATUS_TEMPLATES_JSON`` is validated when settings load, so a typo is loud."""

import json

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings.base import _parse_order_templates


def test_empty_means_no_templates():
    assert _parse_order_templates("") == {}
    assert _parse_order_templates("   ") == {}


def test_a_valid_map_is_returned():
    raw = '{"CONFIRMED": {"en": "order_confirmed_en", "hi": "order_confirmed_hi"}}'
    assert _parse_order_templates(raw) == {
        "CONFIRMED": {"en": "order_confirmed_en", "hi": "order_confirmed_hi"}
    }


def test_the_full_twelve_template_map_is_valid():
    statuses = ["CONFIRMED", "OUT_FOR_DELIVERY", "DELIVERED", "CANCELLED"]
    data = {s: {lang: f"iva_{s.lower()}_{lang}" for lang in ("en", "hi", "te")} for s in statuses}
    assert _parse_order_templates(json.dumps(data)) == data


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        '"x"',
        '{"CONFIRMED": "x"}',
        '{"CONFIRMED": {"en": ""}}',
        # Spelling: loud at startup instead of a silent "no template" at send time.
        '{"Confirmed": {"en": "order_confirmed_en"}}',  # wrong case
        '{"OUT-FOR-DELIVERY": {"en": "order_out_en"}}',  # hyphens
        '{"SHIPPED": {"en": "order_shipped_en"}}',  # not an order status
        '{"CONFIRMED": {"EN": "order_confirmed_en"}}',  # wrong case
        '{"CONFIRMED": {"fr": "order_confirmed_fr"}}',  # unknown language
        '{"CONFIRMED": {"en": "Order_Confirmed_en"}}',  # upper case in the name
        '{"CONFIRMED": {"en": "order-confirmed-en"}}',  # hyphens in the name
        '{"CONFIRMED": {"en": "order confirmed"}}',  # space in the name
    ],
)
def test_a_malformed_map_is_refused_without_echoing_it(bad):
    with pytest.raises(ImproperlyConfigured) as caught:
        _parse_order_templates(bad)
    message = str(caught.value)
    assert bad not in message
    for secret_part in ("Confirmed", "OUT-FOR-DELIVERY", "SHIPPED", "order-confirmed-en", "fr"):
        if secret_part in bad:
            assert f'"{secret_part}"' not in message and secret_part not in message.split()


def test_importing_the_iva_language_list_does_not_replace_djangos_languages_setting():
    from django.conf import global_settings, settings

    assert settings.LANGUAGES == global_settings.LANGUAGES  # (code, name) pairs, not ("en", ...)
