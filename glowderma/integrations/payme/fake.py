"""Deterministic Payme for tests, the demo seed and local end-to-end runs (``PAYME_MODE=fake``).

Scenario switch ``payme.transaction_id``: a fixed Payme transaction id for ``dev/…/simulate/``.

Refunds are manual: no adapter call, the refund is created ``manual_required`` and resolved by staff (or by
the seed, standing in for Payme's ``CancelTransaction``). Payment confirmation data only — transaction ids
(design 03 §7.8): ``PME-`` + ``str(7_741_000_000 + order_id × 37)[:10]`` unless ``scenario["transaction_id"]``
is set.
"""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from . import protocol

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

    def checkout_url(self, account_ref: str, amount_tiyin: int, lang: str, return_url: str) -> str:
        return protocol.checkout_url(
            str(settings.PAYME_CHECKOUT_URL),
            merchant_id=str(settings.PAYME_MERCHANT_ID),
            account_field=str(settings.PAYME_ACCOUNT_FIELD),
            account_ref=account_ref,
            amount_tiyin=amount_tiyin,
            lang=lang,
            return_url=return_url,
        )
