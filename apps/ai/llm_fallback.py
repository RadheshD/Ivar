"""The LLM fallback stage: guard -> classify -> validate -> outcome.

``LLMFallback.run`` never raises and never logs customer text. It returns an outcome
category and, when the model's reply was acceptable, the validated ``Classification``.
Whether and how to act on it is the pipeline's (and then the engine's) decision.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from django.conf import settings

from .classifier import (
    CatalogEntry,
    Classification,
    ClassifierError,
    ClassifierInvalid,
    ClassifierTimeout,
    IntentClassifier,
    parse_classification,
    redact,
)
from .intents import RECOMMEND_INTENT_NAMES, LLMOutcome
from .llm_guard import LLMGuard
from .recommend import Extraction, constraint_keys, parse_extraction

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 4.0
DEFAULT_MIN_CONFIDENCE = 0.7
# Backstop so an adapter that ignores its own timeouts cannot block the caller.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="iva-llm-cls")
_BACKSTOP_SECONDS = 1.0


@dataclass(frozen=True)
class LLMContext:
    """What the engine hands the pipeline when (and only when) the LLM may be used."""

    catalog: tuple[CatalogEntry, ...]
    customer_key: str  # an opaque backend customer ID, never a phone number
    max_quantity: int
    # (slug, name) of every category and group; used only by the recommendation call.
    categories: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ExtractionResult:
    outcome: LLMOutcome
    extraction: Extraction | None = None


@dataclass(frozen=True)
class FallbackResult:
    outcome: LLMOutcome
    classification: Classification | None = None


def _float_setting(name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class LLMFallback:
    def __init__(
        self,
        classifier: IntentClassifier,
        guard: LLMGuard | None = None,
        *,
        min_confidence: float | None = None,
        timeout: float | None = None,
        recommend: bool = False,
    ):
        self.recommend = recommend and hasattr(classifier, "extract")
        self.classifier = classifier
        self.guard = guard if guard is not None else LLMGuard()
        self.min_confidence = (
            min_confidence
            if min_confidence is not None
            else _float_setting("IVA_LLM_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
        )
        self.timeout = (
            timeout
            if timeout is not None
            else _float_setting("IVA_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        )

    def run(self, text: str, ctx: LLMContext) -> FallbackResult:
        message = redact(text)
        if not message:
            return FallbackResult(LLMOutcome.SKIPPED)
        ok, reason = self.guard.allow(ctx.customer_key)
        if not ok:
            logger.info("llm: skipped reason=%s", reason)
            return FallbackResult(LLMOutcome.SKIPPED)

        started = time.monotonic()
        reply = None
        outcome = LLMOutcome.ERROR
        error_kind = ""
        try:
            future = _EXECUTOR.submit(
                self.classifier.classify, message, catalog=ctx.catalog, timeout=self.timeout
            )
            try:
                reply = future.result(timeout=self.timeout + _BACKSTOP_SECONDS)
            except TimeoutError:
                future.cancel()
                raise ClassifierTimeout() from None
        except ClassifierTimeout:
            outcome, error_kind = LLMOutcome.TIMEOUT, "timeout"
        except ClassifierError as exc:
            outcome, error_kind = LLMOutcome.ERROR, exc.kind
        except Exception as exc:
            outcome, error_kind = LLMOutcome.ERROR, type(exc).__name__

        classification = None
        if reply is not None:
            try:
                classification = parse_classification(
                    reply.text,
                    allowed_slugs=frozenset(e.slug for e in ctx.catalog),
                    max_quantity=ctx.max_quantity,
                    extra_intents=RECOMMEND_INTENT_NAMES if self.recommend else (),
                )
            except ClassifierInvalid as exc:
                outcome = LLMOutcome(exc.kind)
                error_kind = exc.kind
            else:
                outcome = self._judge(classification)
        self.guard.record(classification is not None)
        self._log(outcome, error_kind, classification, reply, started)
        return FallbackResult(outcome, classification)

    def extract(self, text: str, ctx: LLMContext) -> ExtractionResult:
        """The recommendation extraction call (one Groq call, counted by the guard)."""
        message = redact(text)
        if not message or not self.recommend:
            return ExtractionResult(LLMOutcome.SKIPPED)
        ok, reason = self.guard.allow(ctx.customer_key)
        if not ok:
            logger.info("llm: recommend skipped reason=%s", reason)
            return ExtractionResult(LLMOutcome.SKIPPED)
        started = time.monotonic()
        reply = None
        outcome, error_kind = LLMOutcome.ERROR, ""
        try:
            future = _EXECUTOR.submit(
                self.classifier.extract, message, categories=ctx.categories, timeout=self.timeout
            )
            try:
                reply = future.result(timeout=self.timeout + _BACKSTOP_SECONDS)
            except TimeoutError:
                future.cancel()
                raise ClassifierTimeout() from None
        except ClassifierTimeout:
            outcome, error_kind = LLMOutcome.TIMEOUT, "timeout"
        except ClassifierError as exc:
            outcome, error_kind = LLMOutcome.ERROR, exc.kind
        except Exception as exc:
            outcome, error_kind = LLMOutcome.ERROR, type(exc).__name__
        extraction = None
        if reply is not None:
            try:
                extraction = parse_extraction(
                    reply.text, category_slugs=frozenset(slug for slug, _ in ctx.categories)
                )
            except ClassifierInvalid as exc:
                outcome, error_kind = LLMOutcome(exc.kind), exc.kind
            else:
                outcome = self.judge_extraction(extraction)
        self.guard.record(extraction is not None)
        logger.info(
            "llm: recommend outcome=%s error=%s request_type=%s constraints=%s latency_ms=%d "
            "model=%s prompt_tokens=%s completion_tokens=%s",
            outcome.value,
            error_kind or "-",
            extraction.request_type if extraction else "-",
            constraint_keys(extraction.request) if extraction else [],
            int((time.monotonic() - started) * 1000),
            reply.model if reply else "-",
            reply.prompt_tokens if reply else "-",
            reply.completion_tokens if reply else "-",
        )
        return ExtractionResult(outcome, extraction)

    def judge_extraction(self, extraction: Extraction) -> LLMOutcome:
        """Outcome category of a validated extraction (also used by the eval harness)."""
        if extraction.request_type == "other":
            return LLMOutcome.UNKNOWN
        if extraction.confidence < self.min_confidence:
            return LLMOutcome.LOW_CONFIDENCE
        if extraction.ambiguous:
            return LLMOutcome.AMBIGUOUS
        return LLMOutcome.OK

    def _judge(self, c: Classification) -> LLMOutcome:
        if c.intent == "unknown":
            return LLMOutcome.UNKNOWN
        if c.confidence < self.min_confidence:
            return LLMOutcome.LOW_CONFIDENCE
        if c.ambiguous:
            return LLMOutcome.AMBIGUOUS
        return LLMOutcome.OK

    @staticmethod
    def _log(outcome, error_kind, classification, reply, started) -> None:
        slugs = (
            [i.product_slug for i in classification.items if i.product_slug]
            if classification
            else []
        )
        logger.info(
            "llm: outcome=%s error=%s intent=%s confidence=%s latency_ms=%d model=%s "
            "prompt_tokens=%s completion_tokens=%s slugs=%s",
            outcome.value,
            error_kind or "-",
            classification.intent if classification else "-",
            f"{classification.confidence:.2f}" if classification else "-",
            int((time.monotonic() - started) * 1000),
            reply.model if reply else "-",
            reply.prompt_tokens if reply else "-",
            reply.completion_tokens if reply else "-",
            slugs,
        )
