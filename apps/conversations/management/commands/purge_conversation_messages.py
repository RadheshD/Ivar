from django.core.management.base import BaseCommand, CommandError

from apps.conversations.retention import purge_old_messages, retention_days


class Command(BaseCommand):
    help = "Delete conversation message metadata older than IVA_MESSAGE_RETENTION_DAYS."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None, help="Override the retention.")

    def handle(self, *args, days=None, **options):
        if days is not None and days < 1:
            raise CommandError("--days must be at least 1.")
        deleted = purge_old_messages(days)
        used = days or retention_days()
        self.stdout.write(f"Deleted {deleted} message rows older than {used} days.")
