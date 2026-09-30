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

from apps.iva_common.logging import mask_text

from . import fuzzy, rules
from .intents import Intent, IntentResult, LLMOutcome, Stage, keep_relevant, parse_reply_id
from .language import detect_language
from .llm import LLMClient, LLMTimeout, build_prompt, client_from_settings, timeout_from_settings
from .plan_message import parse_plan_message
from .text import tokenize
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
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        llm_timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    ):
        if not 0 < confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be in (0, 1].")
        if llm_timeout <= 0:
            raise ValueError("llm_timeout must be greater than 0.")
        self._llm = llm
        self._threshold = confidence_threshold
        self._timeout = llm_timeout

    def understand(
        self,
        text: str | None = None,
        *,
        reply_id: str | None = None,
        product_names: Iterable[str] = (),
        category_names: Iterable[str] = (),
    ) -> IntentResult:
        """Classify one inbound message. ``product_names``/``category_names`` are optional
        plain-string catalog hints that improve fuzzy matching."""
        try:
            return self._understand(text, reply_id, product_names, category_names)
        except Exception as exc:
            logger.error("intent pipeline failed: error=%s", type(exc).__name__)
            return IntentResult(Intent.FALLBACK, 0.0, Stage.NONE)

    # ------------------------------------------------------------------ stages

    def _understand(self, text, reply_id, product_names, category_names) -> IntentResult:
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

        tokens = tokenize(text) if text else []
        if not tokens:
            return IntentResult(Intent.FALLBACK, 0.0, Stage.NONE, detected_language=language)

        vocab = rules.Vocabulary.build(product_names, category_names)

        # 2. Rules / keywords / aliases on the words as typed.
        raw = rules.match(tokens, vocab)
        # 3. Fuzzy: correct unknown words and match whole catalog names.
        corrected, changed = fuzzy.correct_tokens(tokens, vocab)
        fuzzed = rules.match(corrected, vocab, fuzzy_hints=True)

        if changed and self._confident(fuzzed):
            return self._decided(fuzzed, Stage.FUZZY, language)
        if self._confident(raw):
            return self._decided(raw, Stage.RULES, language)
        if self._confident(fuzzed):
            return self._decided(fuzzed, Stage.FUZZY, language)

        # 4. LLM fallback, 5. schema validation.
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


def pipeline_from_settings() -> IntentPipeline:
    """Pipeline configured from ``LLM_*`` settings (no LLM when ``LLM_PROVIDER`` is empty)."""
    return IntentPipeline(client_from_settings(), llm_timeout=timeout_from_settings())
