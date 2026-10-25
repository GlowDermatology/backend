from django.apps import AppConfig


class CommerceConfig(AppConfig):
    name = "glowderma.commerce"

    def ready(self) -> None:
        # The outbox handlers (bts.create_shipment, billz.push_sale, refund.submit), their dead-letter hooks
        # and the beat task register at import time; every process that dispatches the outbox needs them.
        from glowderma.commerce import tasks  # noqa: F401
