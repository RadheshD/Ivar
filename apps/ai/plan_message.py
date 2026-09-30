"""Parser for the AI Planner's "Share Plan on WhatsApp" message.

The planner page lives on the website, not in this repo. It sends a customer to
``wa.me/<business number>?text=<url-encoded message>``, and the message's first line
identifies it as a plan for us to parse deterministically, ahead of the rest of the
intent pipeline. Unknown keys and invalid/out-of-range values are dropped silently,
never guessed and never logged: this only restates targets the customer sent, never
computes or fabricates anything. Health-adjacent fields we do not support at all (BMI,
BMR, TDEE, healthy weight range, nutrition score) are simply not recognised as keys, so
they are dropped along with anything else unrecognised.
"""

from __future__ import annotations

from .intents import Entities

PLAN_HEADER = "IVA PLAN v1"
# Generous for a header, four fields and ten picks, but bounded against abuse.
PLAN_MAX_LENGTH = 2000

_KNOWN_GOALS = ("healthy_eating",)
_KNOWN_DIETS = ("veg", "egg", "non_veg")
_CALORIES_RANGE = (800, 6000)
_PROTEIN_RANGE = (10, 400)
_MAX_PICKS = 10
_PICKS_SEPARATOR = "; "


def _parse_int_in_range(value: str, low: int, high: int) -> int | None:
    try:
        n = int(value.strip())
    except ValueError:
        return None
    return n if low <= n <= high else None


def parse_plan_message(text: str) -> Entities | None:
    """Return the parsed ``Entities`` for a plan message, or ``None`` if this isn't one.

    ``None`` means "treat as ordinary text": either the message is oversized, or its
    first line isn't exactly the plan header.
    """
    if not isinstance(text, str) or len(text) > PLAN_MAX_LENGTH:
        return None
    lines = text.splitlines()
    if not lines or lines[0] != PLAN_HEADER:
        return None

    goal: str | None = None
    diet: str | None = None
    calories: int | None = None
    protein_g: int | None = None
    picks: tuple[str, ...] = ()

    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "goal":
            goal = value if value in _KNOWN_GOALS else None
        elif key == "diet":
            diet = value if value in _KNOWN_DIETS else None
        elif key == "calories":
            calories = _parse_int_in_range(value, *_CALORIES_RANGE)
        elif key == "protein_g":
            protein_g = _parse_int_in_range(value, *_PROTEIN_RANGE)
        elif key == "picks":
            names = [p.strip() for p in value.split(_PICKS_SEPARATOR)]
            picks = tuple(p for p in names if p)[:_MAX_PICKS]
        # Any other key (including bmi/bmr/tdee/healthy_weight_range/nutrition_score,
        # and anything unrecognised) is ignored: not stored, not logged.

    return Entities(goal=goal, diet=diet, calories=calories, protein_g=protein_g, picks=picks)
