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

from . import llm_state
from .classifier import (
    HARD_INPUT_CEILING,
    SYSTEM_PROMPT,
    CatalogEntry,
    Classification,
    ClassifierError,
    ClassifierInvalid,
    ClassifierRateLimited,
    ClassifierTimeout,
    IntentClassifier,
    has_request_left,
    parse_classification,
    prepare_input,
    redact,
)
from .intents import RECOMMEND_INTENT_NAMES, LLMOutcome
from .llm_guard import LLMGuard, estimate_tokens
from .recommend import SYSTEM_PROMPT as RECOMMEND_PROMPT
from .recommend import Extraction, constraint_keys, parse_extraction

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 4.0
DEFAULT_MIN_CONFIDENCE = 0.7
# Backstop so an adapter that ignores its own timeouts cannot block the caller.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="iva-llm-cls")
_BACKSTOP_SECONDS = 1.0
DEFAULT_MAX_INPUT_CHARS = 500
_SCHEMA_CHARS = 1500  # the JSON schema travels in every request
_NOT_PROCESSED = ("rate_limited", "connection")  # the provider did no work: no tokens spent
_FAILURE_KINDS = ("timeout", "rate_limited", "http_error", "connection", "bad_response")


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

    # ------------------------------------------------------------------ shared steps

    @staticmethod
    def _prepare(text: str) -> str | None:
        """The message as it may leave the process, or ``None`` (skip the model): normalised,
        within the length limit (never truncated), redacted, with something left to read."""
        try:
            limit = int(getattr(settings, "IVA_LLM_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS))
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_INPUT_CHARS
        prepared = prepare_input(text, limit if limit > 0 else DEFAULT_MAX_INPUT_CHARS)
        if prepared is None:
            llm_state.stat("skipped.input_limit")
            logger.info("llm: skipped reason=input_limit")
            return None
        message = redact(prepared, limit=HARD_INPUT_CEILING)
        if not has_request_left(message):
            llm_state.stat("skipped.redaction_empty")
            logger.info("llm: skipped reason=redaction_empty")
            return None
        return message

    def _admit(self, ctx: LLMContext, est_chars: int, what: str):
        permit = self.guard.acquire(ctx.customer_key, estimate_tokens(est_chars))
        if not permit.ok:
            llm_state.stat(f"skipped.{permit.reason}")
            logger.info("llm: %sskipped reason=%s", what, permit.reason)
            return None
        return permit

    def _invoke(self, fn, *args, **kwargs):
        """Run the provider call under the backstop timeout. Returns ``(reply, outcome, kind,
        rate_limited_exc)``; never raises."""
        try:
            future = _EXECUTOR.submit(fn, *args, **kwargs)
            try:
                return future.result(timeout=self.timeout + _BACKSTOP_SECONDS), None, "", None
            except TimeoutError:
                future.cancel()
                raise ClassifierTimeout() from None
        except ClassifierTimeout:
            return None, LLMOutcome.TIMEOUT, "timeout", None
        except ClassifierRateLimited as exc:
            return None, LLMOutcome.ERROR, exc.kind, exc
        except ClassifierError as exc:
            return None, LLMOutcome.ERROR, exc.kind, None
        except Exception as exc:
            return None, LLMOutcome.ERROR, type(exc).__name__, None

    def _settle(self, permit, reply, error_kind, limited, parsed_ok, rejected_kind, bad_slugs):
        """Account for one finished call: tokens, breaker, window, metrics, provider pause."""
        if limited is not None:
            self.guard.note_rate_limited(limited.retry_after)
        elif reply is not None and reply.pause_s:
            self.guard.note_rate_limited(reply.pause_s)
        if (
            reply is not None
            and reply.prompt_tokens is not None
            and reply.completion_tokens is not None
        ):
            used = reply.prompt_tokens + reply.completion_tokens
        elif error_kind in _NOT_PROCESSED:
            used = 0
        else:
            used = None
        failed = reply is None
        rejected = rejected_kind is not None or bad_slugs > 0
        if failed:
            llm_state.stat(f"failed.{error_kind if error_kind in _FAILURE_KINDS else 'error'}")
        if rejected_kind is not None:
            llm_state.stat(f"rejected.{rejected_kind}")
        if bad_slugs:
            llm_state.stat("rejected.hallucinated_slug")
        self.guard.finish(
            permit, success=parsed_ok, tokens_used=used, failed=failed, rejected=rejected
        )

    def run(self, text: str, ctx: LLMContext) -> FallbackResult:
        message = self._prepare(text)
        if message is None:
            return FallbackResult(LLMOutcome.SKIPPED)
        chars = len(SYSTEM_PROMPT) + 400 + _SCHEMA_CHARS + len(message)
        chars += sum(len(e.slug) + len(e.name) + len(e.category) + 3 for e in ctx.catalog[:150])
        permit = self._admit(ctx, chars, "")
        if permit is None:
            return FallbackResult(LLMOutcome.SKIPPED)

        started = time.monotonic()
        reply, failed_outcome, error_kind, limited = self._invoke(
            self.classifier.classify, message, catalog=ctx.catalog, timeout=self.timeout
        )
        outcome = failed_outcome or LLMOutcome.ERROR
        rejected_kind = None

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
                error_kind = rejected_kind = exc.kind
            else:
                outcome = self._judge(classification)
        self._settle(
            permit, reply, error_kind, limited, classification is not None, rejected_kind,
            classification.rejected_slugs if classification else 0,
        )  # fmt: skip
        self._log(outcome, error_kind, classification, reply, started)
        return FallbackResult(outcome, classification)

    def extract(self, text: str, ctx: LLMContext) -> ExtractionResult:
        """The recommendation extraction call (one Groq call, counted by the guard)."""
        if not self.recommend:
            return ExtractionResult(LLMOutcome.SKIPPED)
        message = self._prepare(text)
        if message is None:
            return ExtractionResult(LLMOutcome.SKIPPED)
        chars = len(RECOMMEND_PROMPT) + 400 + _SCHEMA_CHARS + len(message)
        chars += sum(len(slug) + len(name) + 1 for slug, name in ctx.categories[:100])
        permit = self._admit(ctx, chars, "recommend ")
        if permit is None:
            return ExtractionResult(LLMOutcome.SKIPPED)
        started = time.monotonic()
        reply, failed_outcome, error_kind, limited = self._invoke(
            self.classifier.extract, message, categories=ctx.categories, timeout=self.timeout
        )
        outcome = failed_outcome or LLMOutcome.ERROR
        rejected_kind = None
        extraction = None
        if reply is not None:
            try:
                extraction = parse_extraction(
                    reply.text, category_slugs=frozenset(slug for slug, _ in ctx.categories)
                )
            except ClassifierInvalid as exc:
                outcome, error_kind = LLMOutcome(exc.kind), exc.kind
                rejected_kind = exc.kind
            else:
                outcome = self.judge_extraction(extraction)
        self._settle(permit, reply, error_kind, limited, extraction is not None, rejected_kind, 0)
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

    def health_check(self, text: str, ctx: LLMContext) -> bool:
        """ONE health-only look at a message nothing else understood: True when the model reports
        an emergency. The same input limits, redaction, guard, token budget and kill switch as
        every other call apply; any failure or skip is False. Logs names and counts only."""
        if not hasattr(self.classifier, "extract"):
            return False
        message = self._prepare(text)
        if message is None:
            return False
        chars = len(RECOMMEND_PROMPT) + 400 + _SCHEMA_CHARS + len(message)
        chars += sum(len(slug) + len(name) + 1 for slug, name in ctx.categories[:100])
        permit = self._admit(ctx, chars, "health ")
        if permit is None:
            return False
        reply, _outcome, error_kind, limited = self._invoke(
            self.classifier.extract, message, categories=ctx.categories, timeout=self.timeout
        )
        extraction, rejected_kind = None, None
        if reply is not None:
            try:
                extraction = parse_extraction(
                    reply.text, category_slugs=frozenset(slug for slug, _ in ctx.categories)
                )
            except ClassifierInvalid as exc:
                rejected_kind = exc.kind
        self._settle(permit, reply, error_kind, limited, extraction is not None, rejected_kind, 0)
        emergency = bool(extraction and extraction.request.health_context == "emergency")
        logger.info("llm: health check emergency=%s error=%s", emergency, error_kind or "-")
        return emergency

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
