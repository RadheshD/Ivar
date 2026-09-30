"""Money helpers. Amounts are always ``Decimal``; floats are rejected."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISE = Decimal("0.01")
CURRENCY_SYMBOLS = {"INR": "₹"}


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert ``value`` to ``Decimal``. Floats and bools are refused to avoid rounding errors."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("Money must be Decimal, int or str, never float or bool.")
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError("Invalid money amount.") from exc
    if not result.is_finite():
        raise ValueError("Invalid money amount.")
    return result


def quantize(value: Decimal | int | str) -> Decimal:
    """Round to paise (two decimal places, half-up)."""
    return to_decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def _group_indian(integer_digits: str) -> str:
    """Group digits the Indian way: 12,34,567."""
    if len(integer_digits) <= 3:
        return integer_digits
    head, tail = integer_digits[:-3], integer_digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join(pairs + [tail])


def format_money(value: Decimal | int | str, currency: str = "INR") -> str:
    """Format for customers, e.g. ``₹1,249`` or ``₹1,249.50``. Paise shown only when non-zero."""
    amount = quantize(value)
    sign = "-" if amount < 0 else ""
    integer, _, fraction = f"{abs(amount):.2f}".partition(".")
    text = _group_indian(integer)
    if fraction != "00":
        text = f"{text}.{fraction}"
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{sign}{symbol}{text}"
    return f"{sign}{currency} {text}"


def format_inr(value: Decimal | int | str) -> str:
    return format_money(value, "INR")
