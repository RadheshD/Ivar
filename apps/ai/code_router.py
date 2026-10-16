"""Code-only routing for messages that must never depend on a model.

``route`` runs ahead of the rules and fuzzy stages and returns a ready ``IntentResult`` (a
``RecommendRequest`` the engine answers from fixed templates) for:

* a multi-day plan request  -> the planner hand-off ("7" is never a quantity);
* allergen / ingredient / suitability wording -> a fixed "I don't have that information /
  I can't give medical or dietary advice" reply. Never yes or no, never inferred from names,
  tags, diet type or images;
* a product-fact question (protein, calories, pack size, veg, price) -> answered from the
  Backend Port record only;
* a label or popularity request (vegan, gluten free, best seller ...) -> disclosed, never
  applied silently.

It uses no model and no setting other than ``IVA_SAFETY_KEYWORDS_ENABLED`` (the allergen /
ingredient / suitability replies share the emergency check's switch; default on). A
non-English match is a starter set that needs native-speaker review (see ``code_cues``).
"""

from __future__ import annotations

import re
from dataclasses import replace

from django.conf import settings

from . import code_cues, constraints, rules
from .intents import Entities, Intent, IntentResult, RecommendRequest, Stage
from .recommend import _phrase_matches

# "7 day plan", "diet for 5 days", "7day diet": a day count beside a plan word.
_DAYS_PLAN = re.compile(
    r"\b\d{1,2} ?(?:days?|din|rojula)\b.*\b(?:plan|diet|meal|chart)\b"
    r"|\b(?:plan|diet|meal|chart)\b.*\b\d{1,2} ?(?:days?|din|rojula)\b"
)


def _hit(tokens: list[str], joined: str, table: code_cues.Table) -> bool:
    return any(
        _phrase_matches(tokens, joined, phrase)
        for _review, phrases in table.values()
        for phrase in phrases
    )


def _question_frame(raw: str, tokens: list[str], joined: str) -> bool:
    return "?" in raw or "؟" in raw or _hit(tokens, joined, code_cues.QUESTION_FRAME)


def named_product(tokens: list[str], vocab: rules.Vocabulary) -> str | None:
    """The real product name whose every word is in the message (the longest such name)."""
    words = set(tokens)
    best: tuple[int, str] | None = None
    for name in vocab.product_names:
        parts = rules.normalize(name).split()
        if parts and all(p in words for p in parts) and (best is None or len(parts) > best[0]):
            best = (len(parts), name)
    return best[1] if best else None


def product_phrase(tokens: list[str], vocab: rules.Vocabulary) -> str | None:
    """The words left once question and fact words are set aside: a phrase the Backend Port can
    resolve ("ragi upma"), or ``None``. Product words stay even if they are also fact words."""
    keep = vocab.hint_words
    left = [t for t in tokens if t not in code_cues.QUESTION_FILLER or t in keep]
    # a fact word that is only part of the question ("protein" in "how much protein in ...")
    # is dropped when a longer product phrase remains without it
    stripped = [t for t in left if t not in code_cues.QUESTION_FILLER]
    phrase = " ".join(stripped if stripped else left).strip()
    return phrase[:60] or None


def _request(kind: str, **fields) -> RecommendRequest:
    return RecommendRequest(kind=kind, route="code", **fields)


def _result(request: RecommendRequest, language: str | None) -> IntentResult:
    return IntentResult(
        Intent.PRODUCT_SEARCH, 1.0, Stage.RULES, Entities(), language, request=request
    )


def diet_plan_matches(raw: str, tokens: list[str]) -> bool:
    joined = " ".join(tokens)
    return _hit(tokens, joined, code_cues.DIET_PLAN) or bool(_DAYS_PLAN.search(joined))


def stated_allergy(tokens: list[str]) -> bool:
    return _hit(tokens, " ".join(tokens), code_cues.ALLERGY_STATEMENT)


def _labels(tokens: list[str], joined: str) -> list[str]:
    return [k for k, table in code_cues.LABELS.items() if _hit(tokens, joined, table)]


def _cart_or_menu_request(tokens: list[str], joined: str, vocab: rules.Vocabulary) -> bool:
    """A cart, menu, order, basket or category word (a whole category name counts too)."""
    words = set(tokens)
    named_category = any(
        (parts := rules.normalize(name).split()) and all(p in words for p in parts)
        for name in vocab.category_names
    )
    return named_category or _hit(tokens, joined, code_cues.CART_MENU_WORDS)


def safety_info_matches(
    raw: str, tokens: list[str], vocab: rules.Vocabulary, cons: constraints.Constraints
) -> str | None:
    """``"allergy"`` (a stated allergy), ``"allergen"``, ``"ingredient"``, ``"suitability"`` or
    ``None``. Weak allergen words ("nuts") need a question frame; bare label requests
    ("gluten free snacks") are left to the label disclosure."""
    joined = " ".join(tokens)
    if stated_allergy(tokens):
        return "allergy"
    frame = _question_frame(raw, tokens, joined)
    product = named_product(tokens, vocab)
    labels = _labels(tokens, joined)
    ingredient = _hit(tokens, joined, code_cues.INGREDIENT)
    if _hit(tokens, joined, code_cues.WHATS_IN) and not (
        product is None and _cart_or_menu_request(tokens, joined, vocab)
    ):  # "what's in my cart" / "what's in the menu" are cart and menu requests
        ingredient = True
    strong = _hit(tokens, joined, code_cues.ALLERGEN_STRONG)
    weak = _hit(tokens, joined, code_cues.ALLERGEN_WEAK)
    plain_label = bool(set(labels) - {"allergen_free"}) and not frame and not product
    if plain_label and not ingredient:
        return None  # "gluten free snacks": the label disclosure answers it
    if ingredient:
        return "ingredient"
    if strong or (weak and frame):
        return "allergen"
    if _hit(tokens, joined, code_cues.SUITABILITY) and not (
        cons.max_price or cons.min_price or cons.diet_type or cons.sort
    ):  # "healthy under 250": the price limit is applied; "healthy" alone is never answered
        return "suitability"
    return None


def fact_in(raw: str, tokens: list[str]) -> str | None:
    joined = " ".join(tokens)
    for fact, table in code_cues.FACTS.items():
        if _hit(tokens, joined, table):
            return fact
    return None


def route(
    raw: str,
    tokens: list[str],
    vocab: rules.Vocabulary,
    language: str | None,
    cons: constraints.Constraints,
) -> IntentResult | None:
    """The code-only result for ``raw`` (already tokenised), or ``None``."""
    if not tokens:
        return None
    joined = " ".join(tokens)

    if diet_plan_matches(raw, tokens):
        health = "allergy" if stated_allergy(tokens) else "none"
        return _result(_request("diet_plan", health_context=health), language)

    if getattr(settings, "IVA_SAFETY_KEYWORDS_ENABLED", True):
        kind = safety_info_matches(raw, tokens, vocab, cons)
        if kind is not None:
            mention = named_product(tokens, vocab) or product_phrase(tokens, vocab)
            fact = kind if kind != "allergy" else "allergen"
            if (
                kind == "suitability"
                and not _question_frame(raw, tokens, joined)
                and not (named_product(tokens, vocab))
            ):  # "show healthy snacks": the disclaimer, then the named category (never a claim)
                fact = "suitability_browse"
            request = _request(
                "safety_info",
                asked_fact=fact,
                health_context="allergy" if kind == "allergy" else "none",
                product_mention=None if kind in ("allergy", "suitability") else mention,
            )
            return _result(request, language)

    fact = fact_in(raw, tokens)
    # a price / sort limit means a search ("protein under 300"); a diet word is only a limit when
    # the question is not itself about the diet ("is it veg")
    if fact and not (
        cons.max_price or cons.min_price or cons.sort or (cons.diet_type and fact != "diet_type")
    ):
        mention = named_product(tokens, vocab) or product_phrase(tokens, vocab)
        if mention:
            return _result(
                _request("product_question", asked_fact=fact, product_mention=mention), language
            )

    labels = _labels(tokens, joined)
    popular = _hit(tokens, joined, code_cues.POPULARITY)
    if labels or popular:
        label = "popularity" if popular else labels[0]
        base = _request("recommend", unsupported=label)
        return _result(constraints.overlay(base, cons), language)
    return None


def with_category(request: RecommendRequest, tokens, categories) -> RecommendRequest:
    """``request`` restricted to the Port categories its words name (built-in category words
    and exact category names). Never guesses: no match leaves the request unchanged."""
    if request.category_slug or request.category_slugs or not categories:
        return request
    slug = constraints.category_slug_in(tokens, categories)
    if slug:
        return replace(request, category_slug=slug)
    slugs = constraints.category_slugs_for_terms(tokens, categories)
    return replace(request, category_slugs=slugs) if slugs else request
