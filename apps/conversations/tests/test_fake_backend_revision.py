"""The Fake Backend state file's save counter (revision).

A rewrite of the same size inside the same timestamp tick used to be invisible to another
process (the change signature was only mtime + size). The revision, which only ever goes up,
is part of the signature now. Every test uses a temporary file.
"""

import json
import os

import pytest

from apps.conversations.fake_backend import FakeBackend, _file_revision, clear_state_file

WA = "919876500001"
VARIANT = "seed-mix--std"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "fake_backend.json"


def saved(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def revision(path) -> int:
    return saved(path)["seq"]["revision"]


def test_the_revision_goes_up_by_one_on_every_save(path):
    backend = FakeBackend(state_file=path)
    customer = backend.get_or_create_customer(WA)
    seen = [revision(path)]
    for quantity in (1, 1, 1):
        backend.add_item(customer.id, VARIANT, quantity)
        seen.append(revision(path))
    assert seen == [1, 2, 3, 4]
    backend.get_cart(customer.id)  # a read does not save
    assert revision(path) == 4


def test_the_revision_never_goes_back_across_processes_clear_and_reset(path):
    first, second = FakeBackend(state_file=path), FakeBackend(state_file=path)
    customer = first.get_or_create_customer(WA)
    first.add_item(customer.id, VARIANT, 1)
    second.add_item(customer.id, VARIANT, 1)  # a different process saves on top
    after_two = revision(path)
    assert after_two == 3
    first.reset()  # `first` last saw revision 2: it must still write a higher one
    assert revision(path) == after_two + 1
    clear_state_file(path)  # what reset_dev_data --confirm does
    assert revision(path) == after_two + 2


def test_a_same_size_rewrite_in_the_same_timestamp_tick_is_detected(path):
    server = FakeBackend(state_file=path)
    worker = FakeBackend(state_file=path)
    customer = server.get_or_create_customer(WA)
    server.add_item(customer.id, VARIANT, 1)
    assert [ln.quantity for ln in worker.get_cart(customer.id).lines] == [1]
    stat = path.stat()
    old_sig = (stat.st_mtime_ns, stat.st_size)

    worker.add_item(customer.id, VARIANT, 2)  # 1 -> 3: same byte length
    os.utime(path, ns=(old_sig[0], old_sig[0]))  # ...within the same timestamp tick
    stat = path.stat()
    assert (stat.st_mtime_ns, stat.st_size) == old_sig  # the OLD signature cannot tell
    assert server._stat_sig() != server._file_sig  # the revision can
    assert [ln.quantity for ln in server.get_cart(customer.id).lines] == [3]


def test_an_old_format_file_without_a_counter_loads_and_is_not_rewritten_by_loading(path):
    seed = FakeBackend(state_file=path)
    customer = seed.get_or_create_customer(WA)
    seed.add_item(customer.id, VARIANT, 2)
    data = saved(path)
    del data["seq"]["revision"]  # exactly what an old file looks like
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert _file_revision(path) == 0

    loaded = FakeBackend(state_file=path)
    assert [ln.quantity for ln in loaded.get_cart(customer.id).lines] == [2]
    loaded.get_or_create_customer(WA)  # read-only operations
    loaded.list_categories(None)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before  # untouched

    loaded.add_item(customer.id, VARIANT, 1)  # the first save adds the counter: revision 1
    assert revision(path) == 1


@pytest.mark.parametrize("bad", ["x", -4, True, 1.5, None])
def test_a_nonsense_counter_is_treated_as_revision_zero(path, bad):
    seed = FakeBackend(state_file=path)
    customer = seed.get_or_create_customer(WA)
    data = saved(path)
    data["seq"]["revision"] = bad
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    loaded = FakeBackend(state_file=path)
    assert loaded.get_or_create_customer(WA).id == customer.id  # loaded normally
    loaded.add_item(customer.id, VARIANT, 1)
    assert revision(path) >= 1


def test_the_counter_is_cheap_to_read_and_the_save_stays_atomic(path):
    backend = FakeBackend(state_file=path)
    backend.get_or_create_customer(WA)
    assert path.read_bytes()[:64].count(b"revision") == 1  # inside the first bytes
    assert not path.with_name(path.name + ".tmp").exists()  # temp file replaced, not left
    assert _file_revision(path.with_name("missing.json")) == 0
