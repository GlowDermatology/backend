from django.apps import AppConfig


class CommerceConfig(AppConfig):
    name = "glowderma.commerce"

    def ready(self) -> None:
        # The outbox handlers (bts.create_shipment, billz.push_sale, refund.submit), their dead-letter hooks
        # and the beat task register at import time; every process that dispatches the outbox needs them.
        from glowderma.commerce import selectors, tasks  # noqa: F401
        from glowderma.loyalty import hold_grace

        # me/cashback holds[] shows the payment grace only for orders the expiry job really skips (C§2.9.1).
        hold_grace.register(selectors.orders_in_payment_grace)
