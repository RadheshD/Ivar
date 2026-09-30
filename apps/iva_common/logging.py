"""Log masking for phone numbers and WhatsApp IDs.

WhatsApp IDs are the customer's phone number in international format, so one
pattern covers both. A run of 10-15 digits (optionally with a leading "+" and
single spaces or hyphens between digits) is masked to keep only the first two
and last four digits, e.g. ``919876541234`` -> ``91******1234``.

Over-masking (for example a 10+ digit timestamp) is accepted on purpose: it is
always safer than leaking a phone number.
"""

import logging
import re
import traceback

_PHONE_RE = re.compile(r"(?<![\w+])\+?\d(?:[ -]?\d){9,14}(?![\w])")


def mask_phone(value: str) -> str:
    """Mask a single phone number / WhatsApp ID."""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 6:
        return "*" * len(digits)
    return f"{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"


def mask_text(text: str) -> str:
    """Mask every phone number / WhatsApp ID found in ``text``."""
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), str(text))


class MaskingFilter(logging.Filter):
    """Mask phone numbers in the message, its args, the traceback and stack info.

    Attach it to handlers (not loggers) so records propagated from any logger
    are masked before they are emitted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not drop the record
            message = f"{record.msg} {record.args}"
        record.msg = mask_text(message)
        record.args = None

        if record.exc_info and record.exc_info[0] is not None:
            if not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text = mask_text(record.exc_text)
        elif record.exc_text:
            record.exc_text = mask_text(record.exc_text)

        if record.stack_info:
            record.stack_info = mask_text(record.stack_info)
        return True
