import csv
from dataclasses import replace
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.conversations.catalog_export import (
    COLUMNS,
    collect_catalog,
    is_public_https_url,
    write_csv,
)
from apps.conversations.fake_backend import FakeBackend

BASE = "https://cdn.example.test/assets/products/"
COMMAND = "apps.conversations.management.commands.export_meta_catalog.get_backend"


@pytest.fixture
def backend():
    return FakeBackend(image_base_url=BASE)


def test_row_count_and_formats(backend, tmp_path):
    result = collect_catalog(backend)
    assert result.ok
    assert result.total == len(result.rows) == 55
    ids = [r["id"] for r in result.rows]
    assert len(set(ids)) == len(ids)
    out = tmp_path / "meta_catalog.csv"
    write_csv(result.rows, out)
    with out.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert tuple(reader.fieldnames) == COLUMNS
    assert len(rows) == 55
    for r in rows:
        amount, currency = r["price"].split(" ")
        assert currency == "INR"
        assert len(amount.split(".")[1]) == 2
        Decimal(amount)
        assert r["availability"] in {"in stock", "out of stock"}
        assert r["condition"] == "new" and r["brand"] == "Ivar"
        assert r["description"]
        assert r["image_link"].startswith("https://")
        assert r["link"] == "https://ivar-eyw.vercel.app"


def test_availability_follows_stock(backend):
    stock = {p.id: p.in_stock for p in backend._products.values()}
    for r in collect_catalog(backend).rows:
        assert (r["availability"] == "in stock") == stock[r["id"]]


def test_missing_image_is_reported():
    result = collect_catalog(FakeBackend())  # no image base URL -> image_url None
    assert not result.ok
    assert len(result.problems) == 55
    assert all("missing image_link" in p for p in result.problems.values())


def test_non_public_image_is_reported(backend):
    pid = next(iter(backend._products))
    backend._products[pid] = replace(backend._products[pid], image_url="http://localhost/x.jpg")
    result = collect_catalog(backend)
    assert list(result.problems) == [pid]
    assert result.total == 55 and len(result.rows) == 54


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://ivar-eyw.vercel.app/a.jpg", True),
        ("http://ivar-eyw.vercel.app/a.jpg", False),
        ("https://localhost/a.jpg", False),
        ("https://127.0.0.1/a.jpg", False),
        ("https://192.168.1.5/a.jpg", False),
        ("/assets/a.jpg", False),
        ("", False),
        (None, False),
    ],
)
def test_public_https(url, ok):
    assert is_public_https_url(url) is ok


def test_command_fails_and_lists_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(COMMAND, lambda: FakeBackend())
    out = tmp_path / "x.csv"
    first_id = next(iter(FakeBackend()._products))
    with pytest.raises(CommandError, match=first_id):
        call_command("export_meta_catalog", output=str(out))
    assert not out.exists()


def test_command_writes_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(COMMAND, lambda: FakeBackend(image_base_url=BASE))
    out = tmp_path / "x.csv"
    call_command("export_meta_catalog", output=str(out))
    assert "Exported: 55" in capsys.readouterr().out
    assert len(out.read_text(encoding="utf-8").splitlines()) == 56
