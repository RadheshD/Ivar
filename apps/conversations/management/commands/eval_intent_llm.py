"""Development only: score the LLM layer against the synthetic golden sets.

``--set intents`` (default) runs ``apps/ai/golden/intent_golden.json`` through the Groq
classifier; ``--set recommend`` runs ``recommend_golden.json`` through the recommendation
extraction. For every entry it records the model's raw answer, what our validation kept or
dropped, and the FINAL ENGINE ACTION the real decision code takes with that answer (against
the Fake Backend standing in for the Backend Port), then prints accuracy per language and a
SAFETY section. Any safety violation makes the command exit non-zero.

* The eval runs on its own in-memory Fake Backend: it never reads or writes the dev state
  file (``IVA_FAKE_BACKEND_STATE_FILE``). INFO log lines are silenced unless ``--verbose``.
* Console output is safe on cp1252 consoles (unencodable characters are escaped); the full
  results, with Hindi/Telugu intact, go to a UTF-8 JSON file (``--output``).
* ``--ids g005,g015`` runs only those entries; ``--failures-only --from-results FILE``
  re-runs only the entries that failed in a saved results file.
* ``--dry-run`` uses canned replies and makes no network calls (no key needed).
* Without ``--dry-run`` it calls Groq live and spends quota: try ``--limit`` first.
  Output contains only golden-set text, never customer data, and never the key.

Refuses to run unless ``settings.IVA_DEV_RESET_ALLOWED`` is on (dev settings only).
"""

from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.ai import classifier as cls
from apps.ai.groq import DEFAULT_MODEL, GroqClassifier
from apps.conversations.intent_eval import (  # noqa: F401  (re-exported for callers/tests)
    GOLDEN,
    HANDOFF_TYPES,
    RECOMMEND_ADVICE_GOLDEN,
    RECOMMEND_DETERMINISM_GOLDEN,
    RECOMMEND_GOALS_GOLDEN,
    RECOMMEND_GOLDEN,
    RECOMMEND_HEALTH_GOLDEN,
    RECOMMEND_PRICE_GOLDEN,
    RECOMMEND_RANGE_GOLDEN,
    SET_NAMES,
    EvalIsolationError,
    Harness,
    canned_reply,
    catalog_from_backend,
    console_safe,
    is_recommend,
    load_entries,
    load_golden,
    load_recommend_golden,
    load_results,
    quiet_logs,
    render_summary,
    results_payload,
    safety_summary,
    score,
    score_recommend,
    write_results,
)

RATE_LIMIT_WAIT_S = 20
RATE_LIMIT_RETRIES = 3
DEFAULT_OUTPUT = Path("exports") / "eval_intent_results.json"


def build_classifier(key: str, model: str) -> GroqClassifier:
    """The live Groq client. A seam so tests can supply a mocked HTTP transport."""
    return GroqClassifier(key, model)


class Command(BaseCommand):
    help = "Dev only: evaluate the Groq intent layer against the golden sets (live calls)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="Only the first N entries.")
        parser.add_argument("--sleep", type=float, default=3.0, help="Seconds between calls.")
        parser.add_argument(
            "--set", dest="golden_set", choices=SET_NAMES, default=None,
            help="Which golden set to score (default: intents).",
        )  # fmt: skip
        parser.add_argument(
            "--output", default=str(DEFAULT_OUTPUT), help="UTF-8 JSON results file."
        )
        parser.add_argument("--ids", default="", help="Comma-separated golden ids to run.")
        parser.add_argument(
            "--failures-only", action="store_true",
            help="Re-run only entries that failed in --from-results.",
        )  # fmt: skip
        parser.add_argument("--from-results", default="", help="A saved results JSON file.")
        parser.add_argument(
            "--quiet", action="store_true", default=True,
            help="Silence the per-call backend INFO logs (the default).",
        )  # fmt: skip
        parser.add_argument(
            "--verbose", action="store_true", help="Show the per-call backend INFO logs."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Canned replies; no network, no key needed."
        )

    def handle(
        self, *args, limit, sleep, golden_set=None, output=str(DEFAULT_OUTPUT), ids="",
        failures_only=False, from_results="", dry_run=False, quiet=True, verbose=False, **options,
    ):  # fmt: skip
        if not getattr(settings, "IVA_DEV_RESET_ALLOWED", False):
            raise CommandError("eval_intent_llm only runs with the development settings.")
        if failures_only and not from_results:
            raise CommandError("--failures-only needs --from-results FILE.")
        prior = None
        if from_results:
            try:
                prior = load_results(Path(from_results))
            except (OSError, ValueError) as exc:
                raise CommandError(f"Cannot read --from-results: {type(exc).__name__}.") from None
        set_name = golden_set or (prior["set"] if prior else "intents")
        if prior and prior["set"] != set_name:
            raise CommandError(f"--from-results is a '{prior['set']}' file, not '{set_name}'.")
        key = getattr(settings, "GROQ_API_KEY", "") or ""
        if not key and not dry_run:
            raise CommandError("GROQ_API_KEY is not set in the environment.")
        model = "dry-run" if dry_run else (getattr(settings, "IVA_LLM_MODEL", "") or DEFAULT_MODEL)
        min_conf = float(getattr(settings, "IVA_LLM_MIN_CONFIDENCE", 0.7))
        timeout = float(getattr(settings, "IVA_LLM_TIMEOUT_S", 4.0))

        entries = self._select(load_entries(set_name), ids, failures_only, prior, limit)
        groq = None if dry_run else build_classifier(key, model)
        records = []
        with quiet_logs(quiet and not verbose):
            try:
                harness = Harness(min_conf)  # in-memory backend: never the dev state file
            except EvalIsolationError as exc:
                raise CommandError(str(exc)) from None
            for n, entry in enumerate(entries):
                if n and groq is not None:
                    time.sleep(sleep)
                if dry_run:
                    text, error = canned_reply(entry, set_name), None
                else:
                    text, error = self._ask(groq, entry["text"], set_name, harness, timeout)
                records.append(harness.evaluate(entry, set_name, text, error))

        for line in render_summary(records, set_name, model):
            self._say(line)
        payload = results_payload(records, set_name, model, dry_run)
        out_path = Path(output)
        write_results(out_path, payload)
        self._say(f"\nFull results written to {out_path}")
        safety = payload["summary"]["safety"]
        if safety["count"]:
            raise CommandError(
                f"Safety check failed: {safety['count']} unsafe entries "
                f"({', '.join(safety['entries'])})."
            )

    # ------------------------------------------------------------------ helpers

    def _say(self, text: str) -> None:
        """Write a line that cannot crash on a cp1252 (or any narrow) console."""
        stream = getattr(self.stdout, "_out", None)
        self.stdout.write(console_safe(text, stream))

    @staticmethod
    def _select(entries, ids, failures_only, prior, limit):
        by_id = {e["id"]: e for e in entries}
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        if failures_only:
            failed = [r["id"] for r in prior["records"] if r.get("failed")]
            wanted = [i for i in failed if not wanted or i in wanted]
            if not wanted:
                return []
        if wanted:
            unknown = [i for i in wanted if i not in by_id]
            if unknown:
                raise CommandError(f"Unknown golden id(s): {', '.join(unknown)}.")
            entries = [by_id[i] for i in dict.fromkeys(wanted)]
        return entries[: limit or None]

    @staticmethod
    def _ask(groq, text, set_name, harness, timeout):
        """One live call with 429 retries. Returns ``(raw_reply_text, error_kind)``."""
        error = "error"
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                if is_recommend(set_name):
                    reply = groq.extract(
                        cls.redact(text), categories=harness.categories, timeout=timeout
                    )
                else:
                    reply = groq.classify(
                        cls.redact(text), catalog=harness.catalog, timeout=timeout
                    )
                return reply.text, None
            except cls.ClassifierRateLimited:
                error = "rate_limited"
                if attempt < RATE_LIMIT_RETRIES:
                    time.sleep(RATE_LIMIT_WAIT_S)
            except cls.ClassifierError as exc:
                return None, exc.kind
        return None, error
