from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.adapters import get_backend
from apps.conversations.catalog_export import collect_catalog, write_csv

DEFAULT_OUTPUT = Path("exports") / "meta_catalog.csv"


class Command(BaseCommand):
    help = "Export all products from the Backend Port to a CSV for Meta Commerce Manager."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="CSV path.")

    def handle(self, *args, output=str(DEFAULT_OUTPUT), **options):
        result = collect_catalog(get_backend())
        if not result.ok:
            for pid, problems in result.problems.items():
                self.stderr.write(f"INVALID {pid}: {', '.join(problems)}")
            self.stdout.write(
                f"Products read: {result.total}. Exported: 0. "
                f"Skipped: {len(result.problems)}. No file written."
            )
            raise CommandError(
                f"{len(result.problems)} product(s) failed validation: "
                + ", ".join(result.problems)
            )
        write_csv(result.rows, Path(output))
        self.stdout.write(
            f"Products read: {result.total}. Exported: {len(result.rows)}. Skipped: 0. "
            f"Wrote {output}."
        )
