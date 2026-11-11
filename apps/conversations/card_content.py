"""Pure helpers for product cards: the card body and the typed-quantity check.

Nothing here calls the Backend Port, an LLM or the network. Every value comes from
catalog data passed in by the engine; a missing value is left out, never invented.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from apps.ai.quantities import number_value
from apps.ai.text import normalize

# Meta's media-carousel card body: 160 characters, at most 2 line breaks (verified 2026-11-05,
# developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-media-carousel-messages/).
CARD_BODY_MAX = 160
CARD_BODY_MAX_BREAKS = 2
TAG_SEPARATOR = " · "
ELLIPSIS = "…"
MIN_NAME_CHARS = 12  # the name keeps at least this many characters before other lines are cut
MAX_TYPED_DIGITS = 9  # longer digit runs are "too many" without ever converting them

# A line provider gets the product and returns one extra body line (or ``None``).
# EMPTY ON PURPOSE: a "why it is healthy" line was explicitly not requested. To add one
# later, append a function here -- this is the only place that needs to change.
ExtraLine = Callable[[object], str | None]
EXTRA_LINE_PROVIDERS: tuple[ExtraLine, ...] = ()


def _cut(text: str, limit: int) -> str:
    """``text`` shortened to ``limit`` characters, ending in an ellipsis when cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + ELLIPSIS


def format_protein(value: Decimal | int | float | None) -> str | None:
    """A catalog protein amount as plain text ("12", "7.5"), or ``None`` if unknown."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    if not number.is_finite():
        return None
    text = format(number.normalize(), "f")
    return text


def build_card_body(
    name: str,
    price_line: str,
    tags: Iterable[str] = (),
    extra_lines: Iterable[str] = (),
    *,
    limit: int = CARD_BODY_MAX,
    max_breaks: int = CARD_BODY_MAX_BREAKS,
) -> str:
    """name / price / tags (joined on one line), then any extra lines.

    Empty tags are dropped. The result never exceeds ``limit`` characters or
    ``max_breaks`` line breaks: surplus extra lines are folded onto the last line, a
    name that is too long is cut with an ellipsis, and only if the rest alone is
    still too long is the tag line cut as well. The price is never cut.
    """
    tag_line = TAG_SEPARATOR.join(" ".join(tag.split()) for tag in tags if tag and tag.strip())
    lines = [line for line in (price_line.strip(), tag_line) if line]
    lines.extend(" ".join(x.split()) for x in extra_lines if x and x.strip())
    while len(lines) > max_breaks:  # name + lines = max_breaks + 1 lines at most
        last = lines.pop()
        lines[-1] = f"{lines[-1]}{TAG_SEPARATOR}{last}"
    clean_name = " ".join(str(name).split())
    rest = "\n".join(lines)
    if not rest:
        return _cut(clean_name, limit)
    # Keep at least a short readable name: if the rest leaves less room than that, cut
    # the last line (tags or extra text) -- never the price, which is the first line.
    room = limit - 1 - min(len(clean_name), MIN_NAME_CHARS)
    if len(rest) > room and len(lines) > 1:
        keep = len("\n".join(lines[:-1])) + 1
        lines[-1] = _cut(lines[-1], room - keep)
        rest = "\n".join(line for line in lines if line)
    return f"{_cut(clean_name, limit - 1 - len(rest))}\n{rest}"


@dataclass(frozen=True)
class CardQuantity:
    """Outcome of reading a typed quantity: ``ok`` (use ``value``), ``invalid`` (zero),
    ``too_large`` (above the maximum) or ``none`` (the text is not a bare number)."""

    status: str
    value: int | None = None


def parse_card_quantity(text: str | None, maximum: int) -> CardQuantity:
    """Read a typed quantity for the card quantity step.

    Accepts a whole message that is one number: digits (any script) or a number word
    ("two", Hindi/Telugu words). Anything else is ``none`` so the normal intent layer
    still sees commands like "menu" or "add 2 oats". Huge digit runs are ``too_large``
    without being converted. ``maximum`` is supplied by the caller (the Port's limit).
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return CardQuantity("none")
    if cleaned.isdecimal():  # any length: a huge run is "too many", never converted
        value = int(cleaned) if len(cleaned) <= MAX_TYPED_DIGITS else maximum + 1
    else:
        if len(cleaned) > 40 or not all(ch.isalnum() or ch == " " for ch in cleaned):
            return CardQuantity("none")  # a sentence, a sign ("-3") or a decimal ("2.5")
        value = number_value(normalize(cleaned))
        if value is None:
            return CardQuantity("none")
    if value < 1:
        return CardQuantity("invalid")
    if value > maximum:
        return CardQuantity("too_large")
    return CardQuantity("ok", value)
