"""Turn every Groq model call off (or back on) at runtime, without a restart.

The flag lives in the shared cache, so with Redis configured every process (web, worker)
sees it on its next call. It stops ONLY model calls: price limits, diet words, sorting,
emergency and medical keyword replies and cart guards are code and keep working (the
allergen caution currently needs the model: known gap). Prints no secrets. Without
REDIS_URL the flag is per-process and this command cannot reach a running server (it
says so).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.ai import llm_state


class Command(BaseCommand):
    help = "Show, switch on (--on) or switch off (--off) the runtime kill switch for the LLM."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--on", action="store_true", help="Stop all model calls now.")
        group.add_argument("--off", action="store_true", help="Allow model calls again.")
        group.add_argument("--status", action="store_true", help="Show the state (default).")

    def handle(self, *args, **options):
        shared = not settings.CACHES["default"]["BACKEND"].endswith("LocMemCache")
        try:
            if options["on"]:
                llm_state.set_manual_kill()
                self.stdout.write("LLM kill switch: ON (manual). No model calls will be made.")
            elif options["off"]:
                llm_state.clear_kill()
                self.stdout.write("LLM kill switch: OFF. Model calls are allowed again.")
            else:
                mode = llm_state.kill_mode()
                if mode == llm_state.MANUAL:
                    self.stdout.write("LLM kill switch: ON (manual).")
                elif mode == llm_state.AUTO:
                    self.stdout.write("LLM kill switch: ON (auto-tripped; clears by itself).")
                else:
                    self.stdout.write("LLM kill switch: OFF.")
                self.stdout.write(f"Provider pause: {'yes' if llm_state.is_paused() else 'no'}")
        except Exception as exc:
            raise CommandError(
                f"The shared store could not be reached ({type(exc).__name__}); nothing changed."
            ) from None
        if not shared:
            self.stdout.write(
                "Note: no shared cache is configured (REDIS_URL unset), so this flag is "
                "per-process and does not reach a running server."
            )
