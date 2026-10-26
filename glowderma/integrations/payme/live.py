"""Live Payme adapter (``PAYME_MODE=live``; customer contract §2.7.2, ``docs/integrations/payme.md``).

Payme's money flow is inbound only (the Merchant API JSON-RPC callbacks are the webhook view) and refunds are
manual (``manual_required``, confirmed by Payme's ``CancelTransaction``), so the live adapter makes no HTTP
call: it builds the checkout URL with the real merchant id and reports whether the callbacks can be served.

Checkout (developer.help.paycom.uz, "Отправка чека по методу GET" and "Песочница", read 2026-10-26):
``<checkout_url>/base64("m=<merchant id>;ac.<field>=<account>;a=<tiyin>;l=<lang>;c=<return url>;ct=<ms>")``.
The checkout host is ``https://checkout.paycom.uz`` in production and ``https://test.paycom.uz`` in the
sandbox; ``PAYME_TEST_MODE`` picks the default and ``PAYME_CHECKOUT_URL`` overrides it.
"""

from __future__ import annotations

from django.conf import settings

from . import protocol

PLACEHOLDER = "change-me"


class LivePayme:
    name = "payme"
    refunds_are_manual = True

    def __init__(self, *, merchant_id: str, key: str, checkout_url: str, account_field: str) -> None:
        self.merchant_id = merchant_id
        self.key = key
        self.checkout_base = checkout_url
        self.account_field = account_field

    @classmethod
    def from_settings(cls) -> LivePayme:
        return cls(
            merchant_id=str(settings.PAYME_MERCHANT_ID),
            key=str(settings.PAYME_KEY),
            checkout_url=str(settings.PAYME_CHECKOUT_URL),
            account_field=str(settings.PAYME_ACCOUNT_FIELD),
        )

    def is_configured(self) -> bool:
        """The merchant id (checkout) and the key (Basic auth of the callbacks) are real values."""
        values = (self.merchant_id.strip(), self.key.strip())
        return all(values) and not any(PLACEHOLDER in value.lower() for value in values)

    def checkout_url(self, account_ref: str, amount_tiyin: int, lang: str, return_url: str) -> str:
        return protocol.checkout_url(
            self.checkout_base,
            merchant_id=self.merchant_id,
            account_field=self.account_field,
            account_ref=account_ref,
            amount_tiyin=amount_tiyin,
            lang=lang,
            return_url=return_url,
        )
