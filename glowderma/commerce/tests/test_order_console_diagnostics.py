"""The order's console page after the UX review: the «Diagnostika» block (status, payment, the shipment's
last BTS error, Billz, links to the order's outbox and webhook rows), the renamed events tab, the shipment
inline's error code and the order list's filters."""

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from glowderma.commerce.admin import OrderAdmin, OrderShipmentInline
from glowderma.commerce.models import Order
from glowderma.core.admin import column_field
from glowderma.core.models import OutboxMessage
from glowderma.core.tests.test_console_i18n import visible_text
from glowderma.delivery.tests.factories import ShipmentFactory
from glowderma.integrations.tests.factories import WebhookInboxFactory
from glowderma.payments.tests.factories import PaymentFactory

pytestmark = pytest.mark.django_db


def _stuck_order() -> Order:
    shipment = ShipmentFactory.create(failed=True)
    order = shipment.order
    payment = PaymentFactory.create(order=order, amount=order.total, confirmed_at=order.paid_at)
    for n, status in enumerate(("dead", "done", "pending")):
        OutboxMessage.objects.create(
            topic="billz.push_sale" if status == "dead" else "push.order_status",
            payload={"order_id": order.pk, "purchase_id": None},
            dedupe_key=f"diag-{order.pk}-{n}",
            status=status,
        )
    OutboxMessage.objects.create(topic="push.order_status", payload={"order_id": 1}, dedupe_key="other")
    WebhookInboxFactory.create(entity_ref=f"payment:{payment.pk}")
    WebhookInboxFactory.create(entity_ref=f"order:{order.pk}")
    WebhookInboxFactory.create(entity_ref="order:999999")
    return order


def test_the_diagnostika_block_explains_a_stuck_order(ops_client: Client) -> None:
    order = _stuck_order()
    page = ops_client.get(reverse("admin:commerce_order_change", args=[order.pk]))
    assert page.status_code == 200
    text = visible_text(page.content)
    assert text.index("Diagnostika") < text.index("Summalar")  # the first tab
    assert "Oxirgi xato: bts_rejected — BTS: region not served" in text
    assert "Yaratib bo‘lmadi" in text and "To‘langan" in text
    assert "3 ta xabar" in text and "1 ta o‘lik" in text and "2 ta provayder chaqiruvi" in text
    assert "Hodisalar" in text and "\nTarix\n" not in f"\n{text}\n"  # the events tab, no clash with history


def test_the_diagnostika_links_are_real_filtered_lists(ops_client: Client) -> None:
    order = _stuck_order()
    from glowderma.commerce import diagnostics

    data = diagnostics.context(order)
    outbox = ops_client.get(data["outbox"]["href"])
    assert outbox.context["cl"].result_count == 3
    shown = " ".join(visible_text(outbox.content).split())
    assert f"Qo‘llangan filtrlar: Buyurtma : №{order.pk}" in shown  # the applied-filters bar
    assert ops_client.get(data["outbox"]["dead_href"]).context["cl"].result_count == 1
    assert ops_client.get(data["webhooks"]["href"]).context["cl"].result_count == 2


def test_a_pickup_order_without_payment_reads_cleanly() -> None:
    from glowderma.commerce import diagnostics
    from glowderma.commerce.tests.factories import OrderFactory

    data = diagnostics.context(OrderFactory.create(pickup=True))
    assert data["payment"]["text"] == "To‘lov hali boshlanmagan"
    assert data["shipment"]["text"] == "Talab qilinmaydi (olib ketish)"
    assert data["outbox"]["total"] == 0 and data["webhooks"]["total"] == 0


def test_the_order_page_stays_a_handful_of_queries(ops_client: Client, django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    order = _stuck_order()
    ops_client.get(reverse("admin:commerce_order_change", args=[order.pk]))  # warm the session
    with django_assert_max_num_queries(60):
        assert ops_client.get(reverse("admin:commerce_order_change", args=[order.pk])).status_code == 200


def test_order_list_filters_and_shipment_inline() -> None:
    model_admin = admin.site._registry[Order]
    assert isinstance(model_admin, OrderAdmin)
    assert model_admin.date_hierarchy is None  # the date ranges replace the three lines of day links
    assert "paid_at" in map(column_field, model_admin.list_filter)
    assert "last_error_code" in OrderShipmentInline.fields
