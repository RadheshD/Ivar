"""Intent pipeline.

    reply ID -> rules/keywords/aliases -> difflib fuzzy -> LLM fallback -> validation

Stops at the first confident stage and records it in ``IntentResult.stage``. Never
raises to the caller: any failure ends in CLARIFY or FALLBACK. Customer text is never
logged, and phone numbers are masked before text is sent to the LLM.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from apps.iva_common.logging import mask_text

from . import constraints, fuzzy, rules
from .intents import (
    RECOMMEND_INTENT_NAMES,
    Entities,
    Intent,
    IntentResult,
    LLMOutcome,
    RecommendRequest,
    Stage,
    keep_relevant,
    parse_reply_id,
)
from .language import detect_language
from .llm import LLMClient, LLMTimeout, build_prompt, client_from_settings, timeout_from_settings
from .llm_fallback import ExtractionResult, FallbackResult, LLMContext, LLMFallback
from .plan_message import parse_plan_message
from .recommend import (
    advice_cue_matches,
    any_explicit_sort,
    apply_goal_sort,
    is_followup,
    medical_matches,
    routing_cue_matches,
    sanitize_for_health,
)
from .text import normalize, tokenize
from .validation import LLMOutputError, ValidatedIntent, parse_llm_output

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_LLM_TIMEOUT_SECONDS = 5.0
MAX_TEXT_LENGTH = 1000

# Backstop only: the LLM call runs here so a client that ignores its timeout cannot block
# the caller. A real provider's HTTP connect/read timeouts are the primary guard; keep
# this pool small and bounded.
LLM_MAX_WORKERS = 4
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS, thread_name_prefix="iva-llm")


class IntentPipeline:
    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        fallback: LLMFallback | None = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        llm_timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    ):
        if not 0 < confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be in (0, 1].")
        if llm_timeout <= 0:
            raise ValueError("llm_timeout must be greater than 0.")
        self._llm = llm
        self._fallback = fallback
        self._threshold = confidence_threshold
        self._timeout = llm_timeout

    @property
    def llm_fallback_enabled(self) -> bool:
        """True when the Groq-style classifier fallback is configured (see IVA_LLM_ENABLED)."""
        return self._fallback is not None

    def understand(
        self,
        text: str | None = None,
        *,
        reply_id: str | None = None,
        product_names: Iterable[str] = (),
        category_names: Iterable[str] = (),
        llm_context: LLMContext | None = None,
    ) -> IntentResult:
        """Classify one inbound message. ``product_names``/``category_names`` are optional
        plain-string catalog hints that improve fuzzy matching. ``llm_context`` is passed
        only when the caller has decided the LLM fallback may run for this message."""
        try:
            return self._understand(text, reply_id, product_names, category_names, llm_context)
        except Exception as exc:
            logger.error("intent pipeline failed: error=%s", type(exc).__name__)
            return IntentResult(Intent.FALLBACK, 0.0, Stage.NONE)

    # ------------------------------------------------------------------ stages

    def _understand(
        self, text, reply_id, product_names, category_names, llm_context=None
    ) -> IntentResult:
        # 0. The AI Planner's "IVA PLAN v1" handoff, ahead of every other stage. Checked
        # against the original text (its own length cap, not MAX_TEXT_LENGTH's).
        if isinstance(text, str):
            plan_entities = parse_plan_message(text)
            if plan_entities is not None:
                return IntentResult(Intent.PLAN_RECEIVED, 1.0, Stage.PLAN, plan_entities)

        text = text[:MAX_TEXT_LENGTH] if isinstance(text, str) else None
        language = detect_language(text)

        # 1. Button / list reply ID.
        if reply_id is not None:
            parsed = parse_reply_id(reply_id)
            if parsed:
                intent, entities = parsed
                return IntentResult(
                    intent, 1.0, Stage.BUTTON, keep_relevant(intent, entities), language
                )
            logger.info("unrecognised reply id; falling back to text")

        tokens = tokenize(constraints.prepare(text)) if text else []
        pair: frozenset[int] = frozenset()
        if text and constraints.has_hyphen_range(text):
            # "250-300": a price range only when the message also holds a price or constraint
            # cue; otherwise the pair is tokenized as before (and kept out of quantities).
            ranged = tokenize(constraints.prepare_ranges(text))
            if constraints.parse(ranged).has_range:
                tokens = ranged
            else:
                pair = constraints.hyphen_pair_indices(text)
        if not tokens:
            return IntentResult(Intent.FALLBACK, 0.0, Stage.NONE, detected_language=language)

        vocab = rules.Vocabulary.build(product_names, category_names)

        # 2. Rules / keywords / aliases on the words as typed.
        raw = rules.match(tokens, vocab, no_quantity=pair)
        # 3. Fuzzy: correct unknown words and match whole catalog names.
        corrected, changed = fuzzy.correct_tokens(tokens, vocab)
        cons = constraints.parse(tokens)
        if cons.cue_indices:  # price, comparator, diet and protein words stay as typed
            corrected = [tokens[i] if i in cons.cue_indices else w for i, w in enumerate(corrected)]
            changed = corrected != tokens
        fuzzed = rules.match(corrected, vocab, fuzzy_hints=True, no_quantity=pair)

        # The rules/fuzzy result that would be returned now, if any.
        decision = None
        if changed and self._confident(fuzzed):
            decision = (fuzzed, Stage.FUZZY)
        elif self._confident(raw):
            decision = (raw, Stage.RULES)
        elif self._confident(fuzzed):
            decision = (fuzzed, Stage.FUZZY)

        # Recommendation flow on (and the engine allows the LLM for this message): a
        # recommendation cue or a health/weight-loss phrase can take priority over a weak
        # rules/fuzzy product-search guess. ``deferred`` keeps that guess as the answer if the
        # flow has nothing to say, so behaviour only changes when the flow actually answers.
        flags_on = self._fallback is not None and self._fallback.recommend and reply_id is None
        recommend_on = flags_on and llm_context is not None
        # A constraint the customer stated (a price limit, a diet, "high protein") is never
        # dropped by a name search: a list-showing guess becomes a recommendation instead,
        # unless the words name one real product ("protein smoothie under 300").
        # This route needs no model: with the flags on it runs in EVERY conversation state
        # (even when the LLM may not be called, e.g. a pending choice), from code-read values.
        constraint_on = flags_on and (bool(cons) or cons.unreadable)
        deferred = None
        via_constraint = False
        if decision is not None:
            found, stage = decision
            list_guess = constraint_on and self._shows_list(found)
            if list_guess:
                exact = self._exact_product(found, cons, vocab)
                if exact is not None:
                    entities = replace(found.entities, product_query=exact)
                    return self._decided(replace(found, entities=entities), stage, language)
            if list_guess or (
                recommend_on and self._recommendation_outranks(text, tokens, found, stage, vocab)
            ):
                deferred = self._decided(found, stage, language)
                via_constraint = list_guess
            else:
                return self._decided(found, stage, language)
        if recommend_on and medical_matches(text) and not any_explicit_sort(text):
            logger.info("pipeline: medical phrase matched; no model call")
            return self._medical_result(language)
        if constraint_on and (via_constraint or decision is None):
            if medical_matches(text) and not any_explicit_sort(text):
                logger.info("pipeline: medical phrase matched; no model call")
                return self._medical_result(language)
            return self._constraint_result(text, tokens, language, llm_context, cons)

        # 4. LLM fallback, 5. schema validation.
        new_path = self._fallback is not None and llm_context is not None and reply_id is None
        if new_path:
            outcome, validated = LLMOutcome.NOT_CALLED, None
            if self._fallback.recommend and routing_cue_matches(text):
                # A recommendation cue: ONE call, straight to the extraction (no classifier).
                er = self._fallback.extract(text, llm_context)
                decided = self._from_extraction(er, language, text=text, route="cue")
                if decided is not None:
                    return decided
                outcome = er.outcome  # "other": carry on with the classifier below
            fb = self._fallback.run(text, llm_context)
            outcome = fb.outcome
            c = fb.classification
            if c is not None and c.intent in RECOMMEND_INTENT_NAMES:
                # The classifier path reaching the flow: a second call, the extraction.
                if fb.outcome is LLMOutcome.OK and self._fallback.recommend:
                    decided = self._from_extraction(
                        self._fallback.extract(text, llm_context),
                        language,
                        text=text,
                        route="classifier",
                    )
                    if decided is not None:
                        return decided
                outcome = LLMOutcome.UNKNOWN  # no usable recommendation-flow request
            else:
                decided = self._from_classification(fb, language, tokens)
                if decided is not None:
                    return decided
            if deferred is not None:
                return deferred  # the flow had no answer: keep the rules' guess
        else:
            outcome, validated = self._ask_llm(text, language, vocab)
        if outcome is LLMOutcome.OK:
            return IntentResult(
                validated.intent,
                validated.confidence,
                Stage.LLM,
                keep_relevant(validated.intent, validated.entities),
                language,
                llm_outcome=outcome,
            )
        if outcome is LLMOutcome.LOW_CONFIDENCE:
            return IntentResult(
                Intent.CLARIFY,
                validated.confidence,
                Stage.LLM,
                keep_relevant(Intent.CLARIFY, validated.entities, validated.intent),
                language,
                suggested_intent=validated.intent,
                llm_outcome=outcome,
            )

        # LLM unavailable or rejected: a low-confidence rules candidate becomes a
        # clarifying question; otherwise FALLBACK. Never a guess.
        use_fuzzed = fuzzed.intent is not None and (changed or raw.intent is None)
        candidate, stage = (fuzzed, Stage.FUZZY) if use_fuzzed else (raw, Stage.RULES)
        if candidate.intent:
            return IntentResult(
                Intent.CLARIFY,
                candidate.confidence,
                stage,
                keep_relevant(Intent.CLARIFY, candidate.entities, candidate.intent),
                language,
                suggested_intent=candidate.intent,
                llm_outcome=outcome,
            )
        entities = keep_relevant(Intent.FALLBACK, raw.entities)
        return IntentResult(
            Intent.FALLBACK, 0.0, Stage.NONE, entities, language, llm_outcome=outcome
        )

    @staticmethod
    def _shows_list(found: rules.RuleMatch) -> bool:
        """Would this rules/fuzzy result show a name-search list (or a category listing)?"""
        e = found.entities
        if e.reference_id:
            return False
        if found.intent in (Intent.PRODUCT_SEARCH, Intent.ADD_TO_CART):
            return bool(e.product_query)
        return found.intent is Intent.CATEGORY_SELECT and bool(e.category_query)

    @staticmethod
    def _exact_product(
        found: rules.RuleMatch, cons: constraints.Constraints, vocab: rules.Vocabulary
    ) -> str | None:
        """The real product name the search words resolve to exactly once the constraint
        words ("under", "veg") are set aside, else ``None``. A category name does not count:
        a category plus a constraint is a filtered recommendation, never an unfiltered browse."""
        words = normalize(found.entities.product_query or "").split()
        for drop in (cons.comparator_words, cons.comparator_words | cons.diet_words):
            name = vocab.products_by_norm.get(" ".join(w for w in words if w not in drop))
            if name:
                return name
        return None

    def _constraint_result(
        self, text, tokens, language, llm_context: LLMContext | None, cons: constraints.Constraints
    ) -> IntentResult:
        """Answer a message with stated constraints through the recommendation flow. The
        extraction call (when it works) supplies the category and anything the code did not
        read; the price, diet and ordering read in code always win. If the model cannot be
        used the request is built from the code-read constraints alone, so a limit is never
        dropped. Logs constraint NAMES only."""
        if cons.unreadable:
            # A price word with no readable number: say so; never a search, never picks.
            logger.info("pipeline: constraint route cues=%s", ["price_unreadable"])
            request = RecommendRequest(
                kind="price_unreadable", route="constraint", cues=("price_unreadable",)
            )
            return IntentResult(
                Intent.PRODUCT_SEARCH, 1.0, Stage.RULES, Entities(), language, request=request
            )
        logger.info("pipeline: constraint route cues=%s", list(cons.names))
        if llm_context is None:  # the model may not be called for this message: code only
            er, categories = ExtractionResult(LLMOutcome.SKIPPED), ()
        else:
            er, categories = self._fallback.extract(text, llm_context), llm_context.categories
        return self.constraint_answer(er, language, text, tokens, categories, cons)

    @classmethod
    def constraint_answer(
        cls, er: ExtractionResult, language, text, tokens, categories, cons
    ) -> IntentResult:
        """The result for a constraint message given the extraction attempt ``er`` (also used
        by the eval harness, so it scores exactly what the pipeline does)."""
        decided = cls._from_extraction(er, language, text=text, route="constraint")
        req = decided.request if decided is not None else None
        if req is not None and req.health_context == "emergency":
            return decided  # an emergency the model saw: nothing else is sent
        if req is not None and req.kind in ("recommend", "medical", "product_question"):
            # Code saw a price / diet / sort cue: whatever request_type the model chose, this
            # is a recommendation with the code-read constraints (health context is kept).
            request = constraints.overlay(
                replace(req, kind="recommend", product_mention=None, asked_fact=None), cons
            )
            base = decided
        elif req is not None and req.kind == "diet_plan":
            request = constraints.overlay(req, cons)
            base = decided
        else:  # no usable extraction (or "clarify"/"followup"): code-read constraints only
            blank = RecommendRequest(kind="recommend", route="constraint")
            # The goal-word sort is read in code too: it must not depend on the model being up.
            request = apply_goal_sort(constraints.overlay(blank, cons), text)
            base = IntentResult(
                Intent.PRODUCT_SEARCH, 1.0, Stage.RULES, Entities(), language,
                llm_outcome=er.outcome,
            )  # fmt: skip
        if request.asks_advice and not advice_cue_matches(text):
            request = replace(request, asks_advice=False)  # the model's flag alone shows nothing
        if request.category_slug is None:
            slug = constraints.category_slug_in(tokens, categories)
            request = replace(request, category_slug=slug)
        return replace(base, request=request)

    @staticmethod
    def _names_in_text(tokens: list[str], vocab: rules.Vocabulary) -> bool:
        """True if every word of some real product or category name is in the message."""
        words = set(tokens)
        for name in (*vocab.product_names, *vocab.category_names):
            parts = normalize(name).split()
            if parts and all(p in words for p in parts):
                return True
        return False

    def _recommendation_outranks(self, text, tokens, found, stage, vocab) -> bool:
        """Should the recommendation flow take priority over this rules/fuzzy result?

        Only over a guess: a product search, or a fuzzy-corrected "add" (the fuzzy stage can
        even invent a quantity: "weight" became "eight"). Every other rules result keeps its
        priority. A health/weight-loss phrase beats any fuzzy guess; a recommendation cue
        loses if the message names a real product or category (so "suggest ragi mix" still
        finds ragi mix).
        """
        if medical_matches(text):
            if any_explicit_sort(text):
                return found.intent is Intent.PRODUCT_SEARCH or stage is Stage.FUZZY
            return found.intent is Intent.PRODUCT_SEARCH or stage is Stage.FUZZY
        guess = found.intent is Intent.PRODUCT_SEARCH or (
            found.intent is Intent.ADD_TO_CART and stage is Stage.FUZZY
        )
        return guess and routing_cue_matches(text) and not self._names_in_text(tokens, vocab)

    @staticmethod
    def _medical_result(language: str | None) -> IntentResult:
        request = RecommendRequest(kind="medical", route="cue_medical")
        return IntentResult(
            Intent.PRODUCT_SEARCH, 1.0, Stage.LLM, Entities(), language,
            llm_outcome=LLMOutcome.OK, request=request,
        )  # fmt: skip

    @staticmethod
    def _from_extraction(
        er: ExtractionResult, language: str | None, *, text: str | None = None, route: str = ""
    ) -> IntentResult | None:
        """Map an extraction to a result. ``None`` = fall through (type "other")."""
        ext = er.extraction
        if ext is not None and ext.request.health_context == "emergency":
            # The model saw an emergency: nothing else is sent, whatever the confidence.
            return IntentResult(
                Intent.PRODUCT_SEARCH, 1.0, Stage.LLM, Entities(), language,
                llm_outcome=er.outcome, request=replace(ext.request, route=route),
            )  # fmt: skip
        if er.outcome is LLMOutcome.UNKNOWN:
            return None  # a valid reply that is not a recommendation-flow request
        if ext is None or er.outcome in (LLMOutcome.SKIPPED, LLMOutcome.NOT_CALLED):
            # Could not ask or could not understand: the existing category browse reply.
            logger.info(
                "pipeline: recommend route=%s outcome=fallback reason=%s", route, er.outcome.value
            )
            return IntentResult(
                Intent.CATEGORY_SELECT, 0.0, Stage.LLM, Entities(), language,
                llm_outcome=er.outcome,
            )  # fmt: skip
        if er.outcome in (LLMOutcome.LOW_CONFIDENCE, LLMOutcome.AMBIGUOUS):
            return IntentResult(
                Intent.PRODUCT_SEARCH, ext.confidence, Stage.LLM, Entities(), language,
                llm_outcome=er.outcome, request=RecommendRequest(kind="clarify", route=route),
            )  # fmt: skip
        request = replace(
            apply_goal_sort(sanitize_for_health(ext.request, text), text), route=route
        )
        if request.asks_advice and not (text and advice_cue_matches(text)):
            request = replace(request, asks_advice=False)  # the model's flag alone shows nothing
        if is_followup(request, text):
            request = RecommendRequest(kind="followup", route=route)
        return IntentResult(
            Intent.PRODUCT_SEARCH, ext.confidence, Stage.LLM, Entities(), language,
            llm_outcome=er.outcome, request=request,
        )  # fmt: skip

    def _from_classification(
        self, fb: FallbackResult, language: str | None, tokens: list[str] | None = None
    ) -> IntentResult | None:
        """Turn an acceptable classifier reply into a result; ``None`` = no usable answer
        (unknown, skipped or failed: the caller's existing no-match handling applies)."""
        c = fb.classification
        if c is None or fb.outcome in (LLMOutcome.UNKNOWN, LLMOutcome.SKIPPED):
            return None
        intent = Intent(c.intent)
        first = c.items[0] if c.items else None
        mention = (first.mention or "") if first else ""
        if fb.outcome is LLMOutcome.AMBIGUOUS and mention:
            # Several products could match: show them (read-only) instead of guessing.
            entities = keep_relevant(Intent.PRODUCT_SEARCH, Entities(product_query=mention))
            return IntentResult(
                Intent.PRODUCT_SEARCH, c.confidence, Stage.LLM, entities, language,
                llm_outcome=fb.outcome,
            )  # fmt: skip
        if fb.outcome is not LLMOutcome.OK:
            return IntentResult(
                Intent.CLARIFY, c.confidence, Stage.LLM, Entities(), language,
                suggested_intent=intent, llm_outcome=fb.outcome,
            )  # fmt: skip
        items = c.items
        if tokens:  # a model must not read a price in the message as a quantity either
            items = tuple(
                replace(i, quantity=constraints.drop_price_quantity(i.quantity, tokens))
                for i in items
            )
        quantity = next((i.quantity for i in items if i.quantity), None)
        entities = keep_relevant(intent, Entities(product_query=mention or None, quantity=quantity))
        return IntentResult(
            intent, c.confidence, Stage.LLM, entities, language,
            llm_outcome=fb.outcome, llm_items=items,
        )  # fmt: skip

    def _confident(self, found: rules.RuleMatch) -> bool:
        return found.intent is not None and found.confidence >= self._threshold

    @staticmethod
    def _decided(found: rules.RuleMatch, stage: Stage, language: str | None) -> IntentResult:
        entities = keep_relevant(found.intent, found.entities)
        return IntentResult(found.intent, found.confidence, stage, entities, language)

    def _ask_llm(
        self, text: str, language: str | None, vocab: rules.Vocabulary
    ) -> tuple[LLMOutcome, ValidatedIntent | None]:
        if self._llm is None:
            return LLMOutcome.NOT_CONFIGURED, None
        prompt = build_prompt(
            mask_text(text),
            language_hint=language,
            product_names=vocab.product_names,
            category_names=vocab.category_names,
        )
        try:
            future = _LLM_EXECUTOR.submit(self._llm.complete, prompt, timeout=self._timeout)
            raw = future.result(timeout=self._timeout)
        except (TimeoutError, LLMTimeout):
            future.cancel()
            logger.warning("LLM fallback failed: outcome=timeout")
            return LLMOutcome.TIMEOUT, None
        except Exception as exc:
            logger.warning("LLM fallback failed: outcome=error error=%s", type(exc).__name__)
            return LLMOutcome.ERROR, None

        try:
            validated = parse_llm_output(raw)
        except LLMOutputError as exc:
            logger.warning("LLM fallback failed: outcome=%s reason=%s", exc.kind, exc.reason)
            return LLMOutcome(exc.kind), None
        if validated.confidence < self._threshold:
            return LLMOutcome.LOW_CONFIDENCE, validated
        return LLMOutcome.OK, validated


def fallback_from_settings() -> LLMFallback | None:
    """The Groq classifier fallback, or ``None`` unless ``IVA_LLM_ENABLED`` and a key is set."""
    from django.conf import settings

    from .groq import DEFAULT_MODEL, GroqClassifier

    if not getattr(settings, "IVA_LLM_ENABLED", False):
        return None
    api_key = getattr(settings, "GROQ_API_KEY", "") or ""
    if not api_key:
        logger.warning("IVA_LLM_ENABLED is on but GROQ_API_KEY is not set; LLM fallback is off")
        return None
    if not getattr(settings, "DEBUG", False) and not int(
        getattr(settings, "IVA_LLM_DAILY_TOKEN_CAP", 0) or 0
    ):
        logger.warning(
            "LLM is enabled with an API key but IVA_LLM_DAILY_TOKEN_CAP is not set; "
            "the built-in default budget applies. Set an explicit daily cap."
        )
    model = getattr(settings, "IVA_LLM_MODEL", "") or DEFAULT_MODEL
    recommend = bool(getattr(settings, "IVA_LLM_RECOMMEND_ENABLED", False))
    classifier = GroqClassifier(
        api_key, model, extra_intents=RECOMMEND_INTENT_NAMES if recommend else ()
    )
    return LLMFallback(classifier, recommend=recommend)


def pipeline_from_settings() -> IntentPipeline:
    """Pipeline configured from ``LLM_*`` / ``IVA_LLM_*`` settings."""
    return IntentPipeline(
        client_from_settings(),
        fallback=fallback_from_settings(),
        llm_timeout=timeout_from_settings(),
    )
