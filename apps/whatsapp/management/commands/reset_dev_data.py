from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.whatsapp.dev_reset import (
    UnsafeEnvironment,
    check_safe_to_run,
    count_celery_queue,
    count_postgres,
    count_redis,
    delete_postgres,
    delete_redis,
    purge_celery_queue,
)


class Command(BaseCommand):
    help = (
        "Dev-only: delete all IVA test data (Postgres rows, Redis keys, queued Celery "
        "tasks). Dry run by default (prints counts, deletes nothing); pass --confirm "
        "to actually delete. Refuses to run against production settings or a real "
        "(non-Fake) backend adapter."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Actually delete. Without this flag, only a dry run is performed.",
        )

    def handle(self, *args, confirm=False, **options):
        try:
            check_safe_to_run()
        except UnsafeEnvironment as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("Found (nothing deleted yet):")
        self._report(count_postgres(), count_redis(), count_celery_queue())

        if not confirm:
            self.stdout.write("\nDry run only: nothing was deleted.")
            self.stdout.write("Re-run with --confirm to delete.")
            return

        self.stdout.write("\nDeleting (--confirm)...")
        self._report(delete_postgres(), delete_redis(), purge_celery_queue())
        self.stdout.write("\nDone.")

    def _report(self, pg_counts: dict, redis_counts: dict, celery_count: int) -> None:
        self.stdout.write("  Postgres:")
        for table, n in pg_counts.items():
            self.stdout.write(f"    {table}: {n} row(s)")
        self.stdout.write("  Redis:")
        for label, n in redis_counts.items():
            self.stdout.write(f"    {label}: {n} key(s)")
        self.stdout.write(f"  Celery queued tasks: {celery_count}")
