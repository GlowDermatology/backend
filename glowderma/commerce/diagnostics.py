"""The «Diagnostika» block on top of an order's console page (glowderma.commerce.admin.OrderAdmin): why is
this order stuck? Status, payment, shipment (with its last BTS error), the Billz sale, and links to the
order's outbox messages and provider webhooks. Read-only; a handful of bounded statements (payments,
shipment, purchase, outbox counts, webhooks), never one per row.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.safestring import SafeString
from unfold.utils import display_for_label

from glowderma.commerce.models import Order
from glowderma.core.admin import DANGER, INFO, PRIMARY, SUCCESS, WARNING
from glowderma.core.labels import ORDER_STATUS, PAYMENT_STATUS, stamp
from glowderma.core.models import OutboxMessage
from glowderma.delivery.admin import SHIPMENT_STATE_VARIANTS, bts_status_text
from glowderma.delivery.models import Shipment
from glowderma.integrations.models import WebhookInbox
from glowderma.loyalty.models import Purchase
from glowderma.payments.models import Payment

TEMPLATE = "admin/commerce/order_diagnostics.html"
ORDER_VARIANTS = {
    "awaiting_payment": WARNING,
    "processing": INFO,
    "handed_to_courier": PRIMARY,
    "delivered": SUCCESS,
    "cancelled": DANGER,
}
PAYMENT_VARIANTS = {"pending": WARNING, "paid": SUCCESS, "failed": DANGER, "refunded": INFO}
OPEN_OUTBOX = ("pending", "failed", "in_flight")


def _badge(value: str, text: str, variants: dict[str, str]) -> SafeString:
    rendered: SafeString = display_for_label((value, text), "-", variants)
    return rendered


def _change(model: Any, pk: Any) -> str:
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=[pk])


def _payment(order: Order) -> dict[str, Any]:
    payment = Payment.objects.filter(order=order).order_by("-created_at", "-id").first()
    if payment is None:
        return {"badge": None, "text": "To‘lov hali boshlanmagan", "href": None, "problem": False}
    parts = [payment.get_provider_display()]
    if payment.confirmed_at:
        parts.append(f"tasdiqlangan: {stamp(payment.confirmed_at)}")
    if payment.error_code:
        parts.append(f"xato kodi: {payment.error_code}")
    return {
        "badge": _badge(payment.status, PAYMENT_STATUS.get(payment.status, payment.status), PAYMENT_VARIANTS),
        "text": " · ".join(str(part) for part in parts),
        "href": _change(Payment, payment.pk),
        "problem": payment.status == "failed",
    }


def _shipment(order: Order) -> dict[str, Any]:
    if order.fulfilment != "delivery":
        return {
            "badge": None,
            "text": "Talab qilinmaydi (olib ketish)",
            "href": None,
            "error": "",
            "problem": False,
        }
    shipment = Shipment.objects.filter(order=order).first()
    if shipment is None:
        return {
            "badge": None,
            "text": "Jo‘natma hali yaratilmagan",
            "href": None,
            "error": "",
            "problem": False,
        }
    parts = []
    if shipment.tracking_number:
        parts.append(f"trek: {shipment.tracking_number}")
    if shipment.bts_status:
        parts.append(f"BTS: {bts_status_text(shipment.bts_status)}")
    if shipment.last_attempt_at:
        parts.append(f"oxirgi urinish: {stamp(shipment.last_attempt_at)} ({shipment.attempts} ta urinish)")
    error = ""
    if shipment.last_error_code or shipment.last_error:
        error = " — ".join(part for part in (shipment.last_error_code, shipment.last_error[:200]) if part)
    return {
        "badge": _badge(shipment.state, str(shipment.get_state_display()), SHIPMENT_STATE_VARIANTS),
        "text": " · ".join(parts) or "—",
        "href": _change(Shipment, shipment.pk),
        "error": error,
        "problem": shipment.state in ("failed", "not_created") and bool(error),
    }


def _billz(order: Order, dead_pushes: int) -> dict[str, Any]:
    purchase = Purchase.objects.filter(order=order).first()
    if purchase is None:
        text = "Xarid yozuvi yo‘q (to‘lovdan keyin yaratiladi)"
        return {"text": text, "href": None, "problem": False, "done": False}
    if purchase.billz_pushed_at is not None:
        text = f"Billz’ga yozilgan: {stamp(purchase.billz_pushed_at)}"
        if purchase.billz_sale_id:
            text += f" · sotuv {purchase.billz_sale_id}"
        return {"text": text, "href": _change(Purchase, purchase.pk), "problem": False, "done": True}
    text = "Billz’ga hali yozilmagan"
    if purchase.billz_sale_id:
        text += f" · qoralama {purchase.billz_sale_id}"
    if dead_pushes:
        text += " · yuborish o‘lik xabarda to‘xtagan"
    return {"text": text, "href": _change(Purchase, purchase.pk), "problem": bool(dead_pushes), "done": False}


def context(order: Order) -> dict[str, Any]:
    outbox = OutboxMessage.objects.filter(payload__order_id=order.pk).aggregate(
        total=Count("id"),
        dead=Count("id", filter=Q(status="dead")),
        open=Count("id", filter=Q(status__in=OPEN_OUTBOX)),
        dead_pushes=Count("id", filter=Q(status="dead", topic="billz.push_sale")),
    )
    payment_ids = list(Payment.objects.filter(order=order).values_list("pk", flat=True))
    refs = [f"order:{order.pk}", *(f"payment:{pk}" for pk in payment_ids)]
    webhooks = WebhookInbox.objects.filter(entity_ref__in=refs).count()
    outbox_url = reverse("admin:core_outboxmessage_changelist")
    webhooks_url = reverse("admin:integrations_webhookinbox_changelist")
    return {
        "status": {
            "badge": _badge(order.status, ORDER_STATUS.get(order.status, order.status), ORDER_VARIANTS),
            "text": f"holat o‘zgargan: {stamp(order.status_changed_at)}" if order.status_changed_at else "",
        },
        "payment": _payment(order),
        "shipment": _shipment(order),
        "billz": _billz(order, int(outbox["dead_pushes"])),
        "outbox": {
            "total": int(outbox["total"]),
            "dead": int(outbox["dead"]),
            "open": int(outbox["open"]),
            "href": f"{outbox_url}?{urlencode({'order': order.pk})}",
            "dead_href": f"{outbox_url}?{urlencode({'order': order.pk, 'status__exact': 'dead'})}",
        },
        "webhooks": {
            "total": webhooks,
            "href": f"{webhooks_url}?{urlencode({'entity_ref__in': ','.join(refs)})}",
        },
    }


def render(order: Order) -> SafeString:
    rendered: SafeString = render_to_string(TEMPLATE, context(order))
    return rendered
