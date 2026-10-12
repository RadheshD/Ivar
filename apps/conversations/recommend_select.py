"""Product selection for recommendations: pure code, Backend Port data only.

No model output is trusted here beyond the already-validated constraints. This module does
not decide what "high protein" or "low calorie" means: it only sorts by the real number and
the reply shows that number. A sort the records cannot support is reported, not faked.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from apps.ai.intents import RecommendRequest

from .backend_port import ProductSummary

# Mirrors the Backend Port's documented ``list_products(diet=...)`` preference semantics:
# veg -> veg only, egg -> veg + egg, non_veg -> everything.
_PREFERENCE_ALLOWS = {
    "veg": {"veg"},
    "egg": {"veg", "egg"},
    "non_veg": {"veg", "egg", "non_veg"},
}
_SORT_FIELD = {
    "protein_desc": ("protein_g", "protein", True),
    "calories_asc": ("calories_kcal", "calories", False),
    "price_asc": ("price", "price", False),
    "price_desc": ("price", "price", True),
}


@dataclass
class Selection:
    picks: list[ProductSummary] = field(default_factory=list)
    matched: int = 0  # how many products passed every hard filter
    unsupported_sort: str | None = None  # label key ("protein"...) when records can't sort
    hidden_by_preference: int = 0  # matches dropped only by the saved food preference


def select_products(
    products: Iterable[ProductSummary],
    request: RecommendRequest,
    *,
    saved_diet: str | None,
    max_picks: int,
) -> Selection:
    """In-stock products that pass the hard filters (diet, price range), sorted by the
    requested key, top ``max_picks``. Category filtering is done by the port query."""
    result = Selection()
    kept: list[ProductSummary] = []
    for product in products:
        if not product.in_stock:
            continue
        if request.diet_type and product.diet_type != request.diet_type:
            continue
        if request.min_price is not None and product.price < request.min_price:
            continue
        if request.max_price is not None and product.price > request.max_price:
            continue
        if not request.diet_type and saved_diet in _PREFERENCE_ALLOWS:
            if product.diet_type not in _PREFERENCE_ALLOWS[saved_diet]:
                result.hidden_by_preference += 1
                continue
        kept.append(product)

    if request.sort in _SORT_FIELD:
        attr, label, descending = _SORT_FIELD[request.sort]
        known = [p for p in kept if getattr(p, attr) is not None]
        unknown = [p for p in kept if getattr(p, attr) is None]
        if known:
            # sorted() is stable: ties keep catalog order. Products whose record has no
            # value for the sort field come last (never ranked, never dropped).
            kept = [*sorted(known, key=lambda p: getattr(p, attr), reverse=descending), *unknown]
        else:
            result.unsupported_sort = label  # no product has the field: do not pretend

    result.matched = len(kept)
    result.picks = kept[: max(1, max_picks)]
    return result
