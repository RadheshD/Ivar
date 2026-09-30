"""Harnesses that let the contract suite run against any Backend Port adapter.

To run the suite against the real backend, write a class with the same members as
``ContractHarness`` and point ``IVA_CONTRACT_HARNESS`` at it, e.g.::

    set IVA_CONTRACT_HARNESS=ivar_backend.tests.iva_harness.RealHarness
    pytest apps/conversations/tests/contract -v

The harness must give each test an isolated backend state (fresh customers are
enough) and provide one in-stock and one out-of-stock variant.
"""

import itertools
from typing import Protocol

from apps.conversations.backend_port import BackendPort
from apps.conversations.fake_backend import FakeBackend


class ContractHarness(Protocol):
    backend: BackendPort

    def new_whatsapp_id(self) -> str:
        """A WhatsApp ID that has never been used with this backend."""

    def in_stock_variant(self) -> str:
        """ID of a variant that can be added to a cart and checked out."""

    def other_in_stock_variant(self) -> str:
        """A second, different in-stock variant."""

    def out_of_stock_variant(self) -> str:
        """ID of a variant whose product is out of stock."""

    def close(self) -> None:
        """Release resources after a test."""


class FakeHarness:
    def __init__(self):
        self.backend = FakeBackend()
        self._ids = itertools.count(1)
        self.backend.set_stock("green-tea", False)

    def new_whatsapp_id(self) -> str:
        return f"91900000{next(self._ids):04d}"

    def in_stock_variant(self) -> str:
        return "ivar-oats--std"

    def other_in_stock_variant(self) -> str:
        return "quinoa--std"

    def out_of_stock_variant(self) -> str:
        return "green-tea--std"

    def close(self) -> None:
        self.backend.reset()


BUILTIN_HARNESSES = {"fake": "apps.conversations.tests.contract.harness.FakeHarness"}
