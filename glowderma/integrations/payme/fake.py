"""Deterministic Payme for tests, the demo seed and local end-to-end runs (``PAYME_MODE=fake``).

Refunds are manual: no adapter call, the refund is created ``manual_required`` and resolved by staff (or by
the seed, standing in for Payme's ``CancelTransaction``). Payment confirmation data only — transaction ids
(design 03 §7.8): ``PME-`` + ``str(7_741_000_000 + order_id × 37)[:10]`` unless ``scenario["transaction_id"]``
is set.
"""

from collections.abc import Mapping
from typing import Any

TXN_BASE = 7_741_000_000


def transaction_id(order_id: int, scenario: Mapping[str, Any] | None = None) -> str:
    override = (scenario or {}).get("transaction_id")
    if override:
        return str(override)
    return "PME-" + str(TXN_BASE + order_id * 37)[:10]


class FakePayme:
    name = "payme"
    refunds_are_manual = True

    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = scenario or {}

    def is_configured(self) -> bool:
        return True
