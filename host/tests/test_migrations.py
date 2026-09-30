import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_no_missing_migrations():
    # Exits with SystemExit(1) if any model change lacks a migration.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
