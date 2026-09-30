from django.core.management.base import BaseCommand

from apps.whatsapp.services import report_stalled_messages


class Command(BaseCommand):
    help = "Log a masked warning for inbound messages claimed but not completed (5+ minutes)."

    def handle(self, *args, **options):
        count = report_stalled_messages()
        self.stdout.write(f"Reported {count} stalled message(s).")
