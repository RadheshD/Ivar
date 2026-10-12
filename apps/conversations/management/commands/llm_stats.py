"""Today's Groq usage and budget. Counts only: no customer text, no identifiers, no secrets."""

from django.core.management.base import BaseCommand, CommandError

from apps.ai import llm_guard, llm_state


class Command(BaseCommand):
    help = "Print today's LLM call, token and fallback totals and how much budget is used."

    def handle(self, *args, **options):
        guard = llm_guard.LLMGuard()
        day = llm_state.today()
        try:
            stats = llm_state.stats_today()
            tokens_today = llm_state.counter(f"{llm_guard.BUDGET_DAY_PREFIX}{day}")
            requests_today = int(llm_guard.cache.get(f"{llm_guard.DAILY_KEY_PREFIX}{day}") or 0)
            mode = llm_state.kill_mode()
            paused = llm_state.is_paused()
        except Exception as exc:
            raise CommandError(
                f"The shared store could not be read ({type(exc).__name__})."
            ) from None
        out = self.stdout.write
        out(f"LLM usage for {day} (UTC)")
        out(f"  kill switch: {mode or 'off'}   provider pause: {'yes' if paused else 'no'}")
        out(f"  requests counted: {requests_today} / {guard.daily_cap}")
        out(
            f"  tokens (reserved/settled): {tokens_today} / {guard.daily_tokens} "
            f"({100 * tokens_today // max(1, guard.daily_tokens)}%)"
        )
        out(f"  per-customer: {guard.customer_daily} calls/day, {guard.customer_tokens} tokens/day")
        for name, value in stats.items():
            if value:
                out(f"  {name}: {value}")
        if not any(stats.values()):
            out("  (no model activity recorded today)")
