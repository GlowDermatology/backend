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
from django.utils.translation import gettext, ngettext
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


def _label(labels: dict[str, str], value: str) -> str:
    """The label of ``value`` in the console's language (the raw value when the mapping has none)."""
    text = labels.get(value)
    return gettext(text) if text else value


def _change(model: Any, pk: Any) -> str:
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=[pk])


def _payment(order: Order) -> dict[str, Any]:
    payment = Payment.objects.filter(order=order).order_by("-created_at", "-id").first()
    if payment is None:
        return {"badge": None, "text": gettext("To‘lov hali boshlanmagan"), "href": None, "problem": False}
    parts = [payment.get_provider_display()]
    if payment.confirmed_at:
        parts.append(gettext("tasdiqlangan: %(at)s") % {"at": stamp(payment.confirmed_at)})
    if payment.error_code:
        parts.append(gettext("xato kodi: %(code)s") % {"code": payment.error_code})
    return {
        "badge": _badge(payment.status, _label(PAYMENT_STATUS, payment.status), PAYMENT_VARIANTS),
        "text": " · ".join(str(part) for part in parts),
        "href": _change(Payment, payment.pk),
        "problem": payment.status == "failed",
    }


def _shipment(order: Order) -> dict[str, Any]:
    if order.fulfilment != "delivery":
        return {
            "badge": None,
            "text": gettext("Talab qilinmaydi (olib ketish)"),
            "href": None,
            "error": "",
            "problem": False,
        }
    shipment = Shipment.objects.filter(order=order).first()
    if shipment is None:
        return {
            "badge": None,
            "text": gettext("Jo‘natma hali yaratilmagan"),
            "href": None,
            "error": "",
            "problem": False,
        }
    parts = []
    if shipment.tracking_number:
        parts.append(gettext("trek: %(number)s") % {"number": shipment.tracking_number})
    if shipment.bts_status:
        parts.append(f"BTS: {bts_status_text(shipment.bts_status)}")
    if shipment.last_attempt_at:
        parts.append(
            ngettext(
                "oxirgi urinish: %(at)s (%(count)s ta urinish)",
                "oxirgi urinish: %(at)s (%(count)s ta urinish)",
                shipment.attempts,
            )
            % {"at": stamp(shipment.last_attempt_at), "count": shipment.attempts}
        )
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
        text = gettext("Xarid yozuvi yo‘q (to‘lovdan keyin yaratiladi)")
        return {"text": text, "href": None, "problem": False, "done": False}
    if purchase.billz_pushed_at is not None:
        text = gettext("Billz’ga yozilgan: %(at)s") % {"at": stamp(purchase.billz_pushed_at)}
        if purchase.billz_sale_id:
            text += " · " + gettext("sotuv %(sale)s") % {"sale": purchase.billz_sale_id}
        return {"text": text, "href": _change(Purchase, purchase.pk), "problem": False, "done": True}
    text = gettext("Billz’ga hali yozilmagan")
    if purchase.billz_sale_id:
        text += " · " + gettext("qoralama %(sale)s") % {"sale": purchase.billz_sale_id}
    if dead_pushes:
        text += " · " + gettext("yuborish o‘lik xabarda to‘xtagan")
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
    changed = (
        gettext("holat o‘zgargan: %(at)s") % {"at": stamp(order.status_changed_at)}
        if order.status_changed_at
        else ""
    )
    return {
        "status": {
            "badge": _badge(order.status, _label(ORDER_STATUS, order.status), ORDER_VARIANTS),
            "text": changed,
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
