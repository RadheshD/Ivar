"""Engine side of the grounded recommendation flow (mixin for ``ConversationEngine``).

The LLM only supplied a validated ``RecommendRequest`` (constraints). Everything shown to the
customer is chosen here in code from Backend Port records and written by message templates.
These replies are read-only: they never touch the cart; the cards carry the ordinary
"Add to cart" buttons of the existing non-LLM flow.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.ai import llm_state
from apps.ai.intents import Intent, RecommendRequest
from apps.ai.recommend import (
    constraint_keys,
    emergency_matches,
    has_food_constraints,
    mapped_categories,
    self_harm_matches,
)

from .backend_port import MAX_PAGE_SIZE, NotFound, ProductDetail, ProductSummary
from .channel import (
    Button,
    CarouselCard,
    Outbound,
    OutButtons,
    OutCarousel,
    OutCTA,
    OutProductCard,
    OutText,
)
from .messages import t
from .recommend_select import select_products
from .state import State

logger = logging.getLogger(__name__)

DEFAULT_MAX_PICKS = 4
# Labels the product records cannot support: disclosed in a fixed sentence, never applied.
_LABEL_KEYS = (
    "vegan", "gluten_free", "sugar_free", "other_label", "allergen_free", "ingredient", "allergen",
)  # fmt: skip
MAX_CANDIDATE_PAGES = 12  # 600 products; the port pages at 50


class RecommendFlow:
    """Methods use the engine's helpers (``self.backend``, ``_text``, ``_button`` ...)."""

    # ------------------------------------------------------------------ dispatch

    def _recommendation(self, c, req: RecommendRequest) -> list[Outbound]:
        kind = req.kind
        llm_state.stat(f"reco.{kind}")
        if req.health_context == "emergency":
            replies = self._emergency_reply(c)
        elif kind == "clarify":
            replies = [self._text(c, "recommend.clarify"), *self._main_menu(c)]
        elif kind == "price_unreadable":
            # A price word with no readable number: say so, no picks, no search claim.
            body = t(c.lang, "price.unreadable")
            replies = [OutButtons(body, (self._menu_button(c),), "price.unreadable")]
        elif kind == "followup":
            # No history is ever sent to the model, so a follow-up is a fresh message with
            # nothing to act on: the neutral reply and the category browse.
            replies = [self._text(c, "search.none_neutral"), *self._main_menu(c)]
        elif kind == "medical":
            replies = [self._text(c, "recommend.medical"), *self._main_menu(c)]
        elif kind == "safety_info":
            replies = self._reco_safety_info(c, req)
        elif kind == "diet_plan":
            replies = [*self._reco_safety_block(c, req), *self._reco_diet_plan(c, req)]
        elif kind == "product_question":
            replies = [*self._reco_safety_block(c, req), *self._reco_question(c, req)]
        else:
            replies = self._reco_compose(c, req)
        self._reco_log(req, replies)
        return replies

    # ------------------------------------------------------------------ safety and composition

    _SAFETY_KEYS = {
        "symptom": ("safety.medical", "safety.see_doctor"),
        "condition": ("safety.medical", "safety.see_doctor"),
        "pregnancy_or_child": ("safety.medical", "safety.see_doctor"),
        "weight_loss": ("safety.medical",),
        "allergy": ("safety.allergy",),
    }

    def _reco_safety_block(self, c, req: RecommendRequest) -> list[Outbound]:
        """Fixed sentences for health wording. Health wording is context only: it adds these
        and never changes which products are chosen or how they are sorted."""
        return [self._text(c, key) for key in self._SAFETY_KEYS.get(req.health_context, ())]

    def _emergency_reply(self, c, key: str = "safety.emergency") -> list[Outbound]:
        body = t(c.lang, key)
        contact = (getattr(settings, "IVA_EMERGENCY_CONTACT_TEXT", "") or "").strip()
        if contact:
            body += "\n" + contact  # the owner's own text; the channel never invents numbers
        return [OutButtons(body, (self._menu_button(c),), key)]

    def _reco_safety_info(self, c, req: RecommendRequest) -> list[Outbound]:
        """Allergen / ingredient / suitability wording: ALWAYS a fixed reply, never yes or no and
        never inferred from names, tags, diet type or images. A named product's card may follow."""
        if req.health_context == "allergy":  # a stated allergy: the allergen line, then the menu
            return [self._text(c, "safety.allergy"), *self._main_menu(c)]
        if req.asked_fact == "suitability":
            return [self._text(c, "safety.medical"), *self._main_menu(c)]
        if req.asked_fact == "suitability_browse":
            # "show healthy snacks": the disclaimer, then the category the words name (picks are
            # restricted to it and nothing is called healthy); no category means the menu.
            if req.category_slug or req.category_slugs:
                return [self._text(c, "safety.medical"), *self._reco_products(c, req)]
            return [self._text(c, "safety.medical"), *self._main_menu(c)]
        replies: list[Outbound] = [self._text(c, "safety.ingredient_info")]
        return [*replies, *(self._reco_named_card(c, req.product_mention) or self._main_menu(c))]

    def _reco_named_card(self, c, mention: str | None) -> list[Outbound]:
        """The card of the ONE product a phrase resolves to through the port, else nothing."""
        if not mention:
            return []
        matches = self.backend.resolve_product_names(mention, limit=3)
        exact = [m for m in matches if self._fold_name(m.name) == self._fold_name(mention)]
        found = exact if len(exact) == 1 else matches
        if len(found) != 1:
            return []
        return self._show_card(c, found[0].id, relist=False)

    def _emergency_check(self, c, inbound) -> list[Outbound] | None:
        """Keyword check on free text, in code, before the rules layer and any model call.
        On by default (``IVA_SAFETY_KEYWORDS_ENABLED``), independent of every LLM flag."""
        if not getattr(settings, "IVA_SAFETY_KEYWORDS_ENABLED", True):
            return None
        text = inbound.text
        if inbound.reply_id or not text or not emergency_matches(text):
            return None
        kind = "self_harm" if self_harm_matches(text) else "emergency"
        logger.info("engine: safety stage=safety kind=%s language=%s", kind, c.lang)
        return self._emergency_reply(c, f"safety.{kind}")

    def _reco_compose(self, c, req: RecommendRequest) -> list[Outbound]:
        """[safety block] then [picks block]. A health-only message gets the safety block
        and the category browse, never picks; picks need explicit food constraints."""
        health = req.health_context
        replies: list[Outbound] = self._reco_safety_block(c, req)
        if health == "allergy":
            return [*replies, *self._main_menu(c)]  # no picks that could imply suitability
        meal_unsupported = bool(req.meal_time and not mapped_categories(req.meal_time))
        if meal_unsupported or req.unsupported == "meal_time":
            replies.append(self._text(c, "meal_time.unsupported"))
            meal_unsupported = True
        if req.unsupported == "popularity":
            replies.append(self._text(c, "popularity.unsupported"))
        food = has_food_constraints(req)
        label = req.unsupported if req.unsupported in _LABEL_KEYS else None
        if label and not food:  # the label is disclosed, never applied; then the category menu
            what = t(c.lang, f"recommend.what.{label}")
            return [
                *replies,
                self._text(c, "recommend.unsupported_menu", what=what),
                *self._main_menu(c),
            ]
        browse_only = health != "none" or meal_unsupported or req.unsupported == "popularity"
        if browse_only and not food:
            return [*replies, *self._main_menu(c)]
        if health == "none" and req.asks_advice:
            replies.append(self._text(c, "advice.disclaimer"))
        elif not food and health == "none":
            # Nothing to apply (no goal, constraint, category or diet word): the category menu,
            # never picks in catalog order.
            return [*replies, self._text(c, "recommend.browse"), *self._main_menu(c)]
        if not food:
            return [*replies, *self._main_menu(c)]
        return [*replies, *self._reco_products(c, req)]

    @staticmethod
    def _reco_log(req: RecommendRequest, replies) -> None:
        """One INFO line per recommendation-flow message: which stage handled it, the
        request type, the constraint names and sort that were applied, and the outcome.
        Never any customer text."""
        keys = [m.key for m in replies if getattr(m, "key", None)]
        picks = sum(
            len(m.cards) if isinstance(m, OutCarousel) else 1
            for m in replies
            if isinstance(m, OutCarousel | OutProductCard)
            and not (isinstance(m, OutProductCard) and any(
                isinstance(o, OutCarousel) for o in replies))
        )  # fmt: skip
        if req.health_context == "emergency":
            outcome = "emergency"
        elif req.kind == "medical":
            outcome = "medical"
        elif req.kind == "clarify":
            outcome = "fallback:clarify"
        elif req.kind == "followup":
            outcome = "fallback:followup"
        elif req.kind == "price_unreadable":
            outcome = "price_unreadable"
        elif any(k and k.startswith("safety.") for k in keys) and not picks:
            outcome = "safety_block"
        elif "recommend.unsupported" in keys or "recommend.unsupported_menu" in keys:
            outcome = "cannot_ground"
        elif "recommend.none" in keys:
            outcome = "no_match"
        elif req.kind == "diet_plan":
            outcome = "planner_handoff"
        else:
            outcome = "picks"
        logger.info(
            "engine: recommend stage=%s kind=%s constraints=%s sort=%s "
            "sort_from_goal=%s outcome=%s picks=%d health=%s cues=%s code=%s model=%s",
            req.route or "-", req.kind, constraint_keys(req),
            req.sort or "-", req.sort_from_goal, outcome, picks, req.health_context,
            list(req.cues), list(req.code_keys), list(req.model_keys),
        )  # fmt: skip

    # ------------------------------------------------------------------ diet plan

    def _reco_diet_plan(self, c, req: RecommendRequest) -> list[Outbound]:
        """No plan and no picks: hand off to the planner. A single-meal goal in the same
        message (a category, price or sort constraint) is handled as a recommendation."""
        replies: list[Outbound] = [self._text(c, "recommend.diet_plan")]
        url = self._planner_url()
        if url:
            body = t(c.lang, "planner.cta_body")
            replies.append(OutCTA(body, t(c.lang, "btn.planner"), url, "planner.cta_body"))
        single_meal = any(
            (req.category_slug, req.max_price, req.min_price, req.sort, req.diet_type)
        )
        if single_meal:
            return [*replies, *self._reco_products(c, req)]
        if not url:
            replies.extend(self._main_menu(c))  # no link configured: say so by omission
        return replies

    # ------------------------------------------------------------------ recommend

    def _reco_candidates(self, category_id: str | None) -> list[ProductSummary]:
        products: list[ProductSummary] = []
        offset = 0
        for _ in range(MAX_CANDIDATE_PAGES):
            page, total = self.backend.list_products(
                category_id=category_id, limit=MAX_PAGE_SIZE, offset=offset
            )
            products.extend(page)
            offset += len(page)
            if not page or offset >= total:
                break
        return products

    def _reco_filtered_candidates(self, req: RecommendRequest) -> list[ProductSummary]:
        """Products for the request. A meal time with categories assigned in
        ``apps.ai.meal_time_map`` narrows to those categories (and to the customer's own
        category if they named one); an empty map changes nothing."""
        slugs = mapped_categories(req.meal_time)
        wanted = list(req.category_slugs) or ([req.category_slug] if req.category_slug else [])
        if not slugs:
            if len(wanted) > 1:
                seen_any: dict[str, ProductSummary] = {}
                for slug in wanted:
                    for product in self._reco_candidates(slug):
                        seen_any.setdefault(product.id, product)
                return list(seen_any.values())
            return self._reco_candidates(wanted[0] if wanted else None)
        if req.category_slug:
            slugs = [req.category_slug] if req.category_slug in slugs else []
        seen: dict[str, ProductSummary] = {}
        for slug in slugs:
            for product in self._reco_candidates(slug):
                seen.setdefault(product.id, product)
        return list(seen.values())

    @staticmethod
    def _reco_diet_label(c, diet_type: str) -> str:
        try:
            return t(c.lang, f"diet.{diet_type}")
        except KeyError:
            return diet_type  # a diet value the channel has no label for: show it as recorded

    def _reco_facts_line(self, c, p: ProductSummary) -> str:
        """Built only from fields the record has; a missing field is simply left out."""
        parts = [p.name, self._money(p.price, p.currency)]
        if p.protein_g is not None:
            parts.append(t(c.lang, "recommend.fact_protein", protein=p.protein_g))
        if p.calories_kcal is not None:
            parts.append(t(c.lang, "recommend.fact_kcal", kcal=p.calories_kcal))
        if p.diet_type:
            parts.append(self._reco_diet_label(c, p.diet_type))
        return " · ".join(parts)

    def _reco_products(self, c, req: RecommendRequest) -> list[Outbound]:
        try:
            candidates = self._reco_filtered_candidates(req)
        except NotFound:
            candidates = []
        max_picks = int(getattr(settings, "IVA_LLM_RECOMMEND_MAX_PICKS", DEFAULT_MAX_PICKS) or 0)
        sel = select_products(
            candidates,
            req,
            saved_diet=c.customer.diet,
            max_picks=max_picks if max_picks > 0 else DEFAULT_MAX_PICKS,
        )
        s = c.session
        s.state = State.CATEGORY
        s.listing = None
        if not sel.picks:
            return self._reco_none(c, req)

        notes: list[Outbound] = []
        # popularity and meal time have their own fixed sentences (see _reco_compose)
        unsupported = (
            req.unsupported if req.unsupported not in ("popularity", "meal_time") else None
        )
        what = sel.unsupported_sort or unsupported
        if what:
            key = f"recommend.what.{what}"
            notes.append(self._text(c, "recommend.unsupported", what=t(c.lang, key)))
        intro = t(c.lang, "recommend.intro", n=len(sel.picks))
        if req.sort == "protein_desc" and not sel.unsupported_sort:
            # States the ordering only, never a benefit.
            intro += "\n" + t(c.lang, "recommend.sorted_protein")
        for line in self._reco_applied_lines(c, req):
            intro += "\n" + line  # what was applied, in code, from the message
        cards = [self._reco_card(c, p, self._reco_facts_line(c, p)) for p in sel.picks]
        replies: list[Outbound] = [*notes]
        carousel = self._reco_carousel(c, sel.picks, intro, cards)
        replies.extend([carousel] if carousel else [OutText(intro, "recommend.intro"), *cards])
        if sel.hidden_by_preference and c.customer.diet:
            diet = t(c.lang, f"diet.{c.customer.diet}")
            replies.append(
                self._text(c, "recommend.hidden", count=sel.hidden_by_preference, diet=diet)
            )
        if self._reco_nutrition_unverified(sel.picks):
            replies.append(self._text(c, "product.unverified"))
        replies.append(self._cards_footer(c, False))
        return replies

    def _reco_applied_lines(self, c, req: RecommendRequest) -> list[str]:
        """Fixed sentences saying which constraints were applied (price bounds, a diet word, the
        categories a category word named). Nothing is applied silently."""
        lines: list[str] = []
        limit = self._price_line(c, req)
        if limit:
            lines.append(limit)
        if req.diet_type:
            lines.append(t(c.lang, "diet.applied", diet=self._reco_diet_label(c, req.diet_type)))
        wanted = list(req.category_slugs) or ([req.category_slug] if req.category_slug else [])
        if wanted:
            names = {cat.id: cat.name for cat in self._all_categories()}
            shown = ", ".join(names[s] for s in wanted if s in names)
            if shown:
                lines.append(t(c.lang, "category.applied", category=shown))
        return lines

    def _price_line(self, c, req: RecommendRequest) -> str | None:
        """States the price bounds that were applied; nothing else."""
        low, high = req.min_price, req.max_price
        if low is not None and high is not None:
            key, values = "recommend.price_range", {"min": low, "max": high}
        elif high is not None:
            key, values = "recommend.max_price", {"max": high}
        elif low is not None:
            key, values = "recommend.min_price", {"min": low}
        else:
            return None
        return t(c.lang, key, **{k: self._plain_amount(v) for k, v in values.items()})

    @staticmethod
    def _plain_amount(value) -> str:
        """A price limit as the customer would write it: 300, not 300.00; 299.5 stays."""
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _reco_card(self, c, p: ProductSummary, facts: str) -> OutProductCard:
        _, buttons = self._card_text_and_buttons(c, p)
        return OutProductCard(facts, buttons, self._card_image_url(p), "recommend.card")

    def _reco_carousel(self, c, picks, intro: str, cards) -> OutCarousel | None:
        if not 2 <= len(picks) <= 10:
            return None
        built = []
        for p, card in zip(picks, cards, strict=True):
            url = self._card_image_url(p)
            if url is None:
                return None
            built.append(CarouselCard(url, card.body[:160], card.buttons))
        return OutCarousel(
            intro, tuple(built), (OutText(intro, "recommend.intro"), *cards), "recommend.intro"
        )

    def _reco_nutrition_unverified(self, picks: list[ProductSummary]) -> bool:
        """True if any shown protein/kcal figure belongs to a product whose nutrition the
        backend has not verified (``nutrition_verified`` lives on the detail record)."""
        for p in picks:
            if p.protein_g is None and p.calories_kcal is None:
                continue
            try:
                if not self.backend.get_product(p.id).nutrition_verified:
                    return True
            except NotFound:
                continue
        return False

    def _reco_none(self, c, req: RecommendRequest) -> list[Outbound]:
        buttons = [self._menu_button(c)]
        group_id = self._reco_group_id(req.category_slug)
        if group_id:
            buttons.append(
                Button(
                    self._reply(c, Intent.CATEGORY_SELECT, group_id), t(c.lang, "btn.categories")
                )
            )
        body = "\n".join([t(c.lang, "recommend.none"), *self._reco_applied_lines(c, req)])
        return [OutButtons(body, tuple(buttons), "recommend.none")]

    def _reco_group_id(self, category_slug: str | None) -> str | None:
        if not category_slug:
            return None
        for cat in self._all_categories():
            if cat.id == category_slug:
                return cat.parent_id or cat.id
        return None

    # ------------------------------------------------------------------ product question

    def _reco_question(self, c, req: RecommendRequest) -> list[Outbound]:
        mention = req.product_mention
        if not mention:
            return [self._text(c, "recommend.clarify"), *self._main_menu(c)]
        matches = self.backend.resolve_product_names(mention, limit=3)
        exact = [m for m in matches if self._fold_name(m.name) == self._fold_name(mention)]
        if len(exact) == 1:
            matches = exact
        if not matches:
            body = t(c.lang, "recommend.no_product")
            return [OutButtons(body, (self._menu_button(c),), "recommend.no_product")]
        if len(matches) > 1:
            return self._search(c, mention)  # several products: let the customer pick
        detail = self.backend.get_product(matches[0].id)
        answer = self._reco_answer(c, detail, req.asked_fact)
        return [*answer, *self._show_card(c, detail.id, relist=False)]

    @staticmethod
    def _fold_name(text: str) -> str:
        return " ".join(text.casefold().split())

    def _reco_answer(self, c, d: ProductDetail, fact: str | None) -> list[Outbound]:
        """The recorded fact, verbatim through a template, or an honest 'I don't have it'."""
        if fact is None:
            return []
        lang, pack = c.lang, d.pack_size
        if fact == "price":
            return [
                self._text(
                    c, "recommend.q_price", name=d.name, price=self._money(d.price, d.currency)
                )
            ]
        if fact == "protein" and d.protein_g is not None:
            key = "recommend.q_protein_pack" if pack else "recommend.q_protein"
            return self._reco_nutrition(c, d, key, name=d.name, protein=d.protein_g, pack=pack)
        if fact == "calories" and d.calories_kcal is not None:
            key = "recommend.q_kcal_pack" if pack else "recommend.q_kcal"
            return self._reco_nutrition(c, d, key, name=d.name, kcal=d.calories_kcal, pack=pack)
        if fact == "pack_size" and pack:
            return [self._text(c, "recommend.q_pack", name=d.name, pack=pack)]
        if fact == "diet_type" and d.diet_type:
            diet = self._reco_diet_label(c, d.diet_type)
            return [self._text(c, "recommend.q_diet", name=d.name, diet=diet)]
        if fact == "description" and d.description:
            return [
                self._text(c, "recommend.q_description", name=d.name, description=d.description)
            ]
        if fact in ("allergen", "ingredient", "suitability", "other"):
            # The port has no allergen/ingredient/suitability field: never answer these.
            return [OutText(t(lang, "recommend.q_missing_team"), "recommend.q_missing_team")]
        return [OutText(t(lang, "recommend.q_missing"), "recommend.q_missing")]

    def _reco_nutrition(self, c, d: ProductDetail, key: str, **values) -> list[Outbound]:
        replies: list[Outbound] = [self._text(c, key, **values)]
        if not d.nutrition_verified:
            replies.append(self._text(c, "product.unverified"))
        return replies
