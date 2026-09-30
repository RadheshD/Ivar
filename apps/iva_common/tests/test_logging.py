import io
import logging

import pytest
from django.conf import settings

from apps.iva_common.logging import MaskingFilter, mask_phone, mask_text

WA_ID = "919876541234"


@pytest.fixture
def masked_logger():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(MaskingFilter())
    logger = logging.getLogger("iva.test.masking")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("919876541234", "91******1234"),
        ("+919876541234", "91******1234"),
        ("9876541234", "98****1234"),
        ("+91 98765 41234", "91******1234"),
        ("+1-415-555-1234", "14*****1234"),
    ],
)
def test_mask_phone_formats(raw, masked):
    assert mask_text(f"from {raw} ok") == f"from {masked} ok"


def test_mask_phone_helper():
    assert mask_phone(WA_ID) == "91******1234"


@pytest.mark.parametrize("text", ["order IVA1001", "qty=3", "₹1,249.50", "line_12", "2026-09-23"])
def test_non_phone_text_untouched(text):
    assert mask_text(text) == text


def test_masks_message_text(masked_logger):
    logger, stream = masked_logger
    logger.info(f"message from {WA_ID}")
    assert WA_ID not in stream.getvalue()
    assert "91******1234" in stream.getvalue()


def test_masks_args(masked_logger):
    logger, stream = masked_logger
    logger.info("wa_id=%s phone=%s", WA_ID, "+91 98765 41234")
    out = stream.getvalue()
    assert WA_ID not in out and "98765 41234" not in out
    assert out.count("91******1234") == 2


def test_masks_dict_args(masked_logger):
    logger, stream = masked_logger
    logger.info("payload %(from)s", {"from": WA_ID})
    assert WA_ID not in stream.getvalue()


def test_masks_traceback(masked_logger):
    logger, stream = masked_logger
    try:
        raise ValueError(f"cannot reply to {WA_ID}")
    except ValueError:
        logger.exception("send failed for %s", WA_ID)
    out = stream.getvalue()
    assert "Traceback" in out and "ValueError" in out
    assert WA_ID not in out
    assert out.count("91******1234") == 2


def test_masks_stack_info():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", None, None)
    record.stack_info = f'  File "x.py", line 1, in send\n    send("{WA_ID}")'
    MaskingFilter().filter(record)
    assert WA_ID not in record.stack_info
    assert "91******1234" in record.stack_info


def test_every_configured_handler_has_masking_filter():
    handlers = settings.LOGGING["handlers"]
    assert handlers
    for name, config in handlers.items():
        assert "mask" in config.get("filters", []), name
    assert settings.LOGGING["filters"]["mask"]["()"] == "apps.iva_common.logging.MaskingFilter"


@pytest.mark.parametrize("name", ["", "django", "django.server", "celery"])
def test_live_logger_handlers_mask(name):
    # pytest attaches its own capture handlers; only check the ones our LOGGING created.
    handlers = [
        h for h in logging.getLogger(name).handlers if not type(h).__module__.startswith("_pytest")
    ]
    assert handlers
    for handler in handlers:
        assert any(isinstance(f, MaskingFilter) for f in handler.filters)
