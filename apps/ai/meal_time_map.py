"""Meal time -> category slugs (DATA ONLY). EMPTY on purpose.

The Backend Port has no meal-time field, and guessing it from product names or tags would
invent a claim about the menu. Until the Ivar team fills this in, a meal-time request is
disclosed ("I can't filter by meal time yet ...") and the rest of the request is applied.
When a list is filled in, it is used as a plain category filter: put the category slugs
(see ``Category.id`` in the Backend Port) that the team has decided belong to that meal.
"""

from __future__ import annotations

MEAL_TIME_CATEGORIES: dict[str, list[str]] = {
    "breakfast": [],
    "lunch": [],
    "dinner": [],
    "snack": [],
}
