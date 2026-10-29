"""``IVA_ORDER_STATUS_TEMPLATES_JSON`` is validated when settings load, so a typo is loud."""

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


@pytest.mark.parametrize(
    "bad", ["not json", "[]", '"x"', '{"CONFIRMED": "x"}', '{"CONFIRMED": {"en": ""}}']
)
def test_a_malformed_map_is_refused_without_echoing_it(bad):
    with pytest.raises(ImproperlyConfigured) as caught:
        _parse_order_templates(bad)
    assert bad not in str(caught.value)
