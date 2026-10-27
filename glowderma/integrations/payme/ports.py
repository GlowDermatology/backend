"""The Payme port (design 02 §2.1–§2.2; customer contract §2.7.2). Payme refunds are manual
(``manual_required``: staff cancel the transaction in the Payme cabinet, Payme confirms with
``CancelTransaction``), so the port has no refund call; the Merchant API callbacks are the M2 webhook view
(``commerce/api/webhooks/payme.py``, helpers in :mod:`.protocol`). ``registry.get_payme(scenario)`` picks the
adapter from ``settings.PAYME_MODE`` (default ``INTEGRATIONS_MODE``)."""

from typing import Protocol


class PaymePort(Protocol):
    refunds_are_manual: bool

    def is_configured(self) -> bool:
        """``True`` when the adapter can serve Payme's callbacks (the fake always can)."""
        ...

    def checkout_url(self, account_ref: str, amount_tiyin: int, lang: str, return_url: str) -> str:
        """The Payme checkout page for ``account_ref`` (customer contract §2.7.2, verify M3)."""
        ...
