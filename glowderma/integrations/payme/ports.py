"""The Payme port (design 02 §2.1–§2.2). Payme refunds are manual in M1 (``manual_required``: staff cancel
the transaction in the Payme cabinet, Payme confirms with ``CancelTransaction``), so the port has no refund
call; ``registry.get_payme(scenario)`` picks the adapter from ``settings.PAYME_MODE`` (default
``INTEGRATIONS_MODE``)."""

from typing import Protocol


class PaymePort(Protocol):
    refunds_are_manual: bool

    def is_configured(self) -> bool:
        """``True`` when the adapter can serve Payme's callbacks (the fake always can)."""
        ...
