"""Status pushes: the handler bodies of ``push.order_status``, ``push.appointment`` and
``push.account_security``."""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.accounts import topics as account_topics
from glowderma.accounts.models import Customer, Device
from glowderma.appointments import selectors as appointment_selectors
from glowderma.appointments.models import OPEN_STATUSES
from glowderma.core import enums, i18n, outbox, timeutils
from glowderma.integrations.fcm.ports import (
    NOT_SENT,
    PAYLOAD_INVALID,
    UNREGISTERED,
    FcmUnavailable,
)
from glowderma.integrations.fcm.registry import get_fcm_port
from glowderma.notifications import selectors, texts, topics
from glowderma.notifications.models import (
    InboxNotification,
    StatusTemplate,
)
from glowderma.notifications.services import push_messages

logger = logging.getLogger("glowderma.notifications.services")


def _deliver(
    customer: Customer,
    *,
    dedupe_key: str,
    kind: str,
    title: str,
    body: str,
    link: dict[str, Any],
    source_type: str,
    source_id: int,
    language: str,
    template_status: str = "",
    data: dict[str, str],
    topic: str,
) -> InboxNotification:
    """Claim protocol (design 03 §3.6): ``get_or_create`` the inbox row, claim it, call FCM outside the
    transaction, record the outcome. A redelivery finds the row settled and does nothing.

    B-W1: the push fans out to **every** pushable device of the customer (one FCM batch, newest device
    first, at most ``selectors.MAX_PUSH_DEVICES``), all carrying the same ``inbox_id``. ``data`` gets the
    row's ``inbox_id`` (C§2.14.2) and the message the channel/priority of its type. The row is ``sent`` when
    at least one device got it; a dead token (``unregistered``) prunes that device; ``payload_invalid``
    fails the row with ``push_error = "payload_invalid"`` and an ERROR log and returns normally (the outbox
    message ends ``done``), never touching a device. ``FcmUnavailable`` gives the claim back and raises
    (outbox retry); when no device got it and one answered ``not_sent`` the claim is given back and the
    message deferred (``outbox.RetryLater``); any other exception (a soft time limit) fails the row
    ``interrupted`` — never resent, never left ``sending`` — and re-raises.
    """
    with transaction.atomic():
        devices = selectors.customer_push_devices(customer.pk)
        inbox, _created = InboxNotification.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "customer": customer,
                "kind": kind,
                "title": title,
                "body": body,
                "link": link,
                "source_type": source_type,
                "source_id": source_id,
                "template_status": template_status,
                "language": language,
                "push_state": (
                    InboxNotification.PushState.QUEUED if devices else InboxNotification.PushState.NO_DEVICE
                ),
            },
        )
        if inbox.push_state != InboxNotification.PushState.QUEUED:
            return inbox
        if not devices:
            inbox.push_state = InboxNotification.PushState.NO_DEVICE
            inbox.save(update_fields=["push_state"])
            return inbox
        inbox.push_state = InboxNotification.PushState.SENDING
        inbox.save(update_fields=["push_state"])
        targets = list(dict.fromkeys((device.fcm_token, device.pk) for device in devices))
    messages = [
        push_messages.typed_message(token, inbox.title, inbox.body, {**data, "inbox_id": str(inbox.pk)})
        for token, _device_id in targets
    ]
    sending = InboxNotification.objects.filter(pk=inbox.pk, push_state=InboxNotification.PushState.SENDING)
    try:
        results = get_fcm_port().send(messages)
    except FcmUnavailable:
        sending.update(push_state=InboxNotification.PushState.QUEUED)
        raise
    except Exception:
        # an outcome nobody knows (a soft time limit, a bug): failed, never resent, never stuck "sending"
        sending.update(push_state=InboxNotification.PushState.FAILED, push_error=push_messages.INTERRUPTED)
        raise
    now = timeutils.now()
    delivered = any(result.ok for result in results)
    for (token, device_id), result in zip(targets, results, strict=True):
        if not result.ok and result.error != NOT_SENT:
            push_messages.after_failure(result, token, device_id, topic, inbox.pk)
    if not delivered and any(result.error == NOT_SENT for result in results):
        sending.update(push_state=InboxNotification.PushState.QUEUED)
        raise outbox.RetryLater(
            push_messages.NOT_SENT_RETRY,
            f"push {inbox.pk} not sent by FCM",
            give_up_after=push_messages.NOT_SENT_GIVE_UP,
        )
    if delivered:
        inbox.push_state, inbox.pushed_at = InboxNotification.PushState.SENT, now
    else:
        inbox.push_state, inbox.push_error = InboxNotification.PushState.FAILED, results[0].error
    inbox.save(update_fields=["push_state", "pushed_at", "push_error"])
    return inbox


def render_template(template: StatusTemplate, language: str, order_id: int) -> tuple[str, str]:
    """``(body, language used)``: the customer's language, else Uzbek; ``{raqam}`` → the order number."""
    text = getattr(template, f"text_{language}", "") if language in enums.ContentLanguage.values else ""
    used = language if text else enums.ContentLanguage.UZ
    return (text or template.text_uz).replace("{raqam}", str(order_id)), used


def order_status_title(status: str, fulfilment: str, language: str) -> str:
    """The customer label of C§4.3 in the customer's language; a delivered pickup order reads "Olib
    ketildi"."""
    if status == enums.OrderStatus.DELIVERED and fulfilment == enums.FulfilmentMethod.PICKUP:
        return i18n.t("label.order_status_pickup.delivered", language)
    return i18n.t(f"label.order_status.{status}", language)


def deliver_order_status(payload: dict[str, Any]) -> InboxNotification | None:
    """``push.order_status {order_id, status, event_id, customer_id}``; one push per order per status.

    Title = the customer label (C§4.3) in the customer's language, body = the admin template text. An order
    created before the customer's history horizon (``Customer.history_from``) belongs to a previous holder
    of the number: no inbox row, no push.
    """
    status = str(payload["status"])
    if status not in enums.TemplateStatus.values:
        raise ValueError(f"No push template for order status {status!r}")
    customer = Customer.objects.filter(pk=payload["customer_id"]).first()
    if customer is None:
        logger.warning("push.order_status for unknown customer %s", payload["customer_id"])
        return None
    order_id = int(payload["order_id"])
    facts = selectors.order_push_facts(order_id)
    if facts is None:
        logger.warning("push.order_status for unknown order %s", order_id)
        return None
    fulfilment, created_at = facts
    if customer.history_from is not None and created_at < customer.history_from:
        logger.info("push.order_status skipped: order %s precedes the history horizon", order_id)
        return None
    template = StatusTemplate.objects.get(status=status)
    language = i18n.normalize_language(customer.language)
    # the row is the customer's, in the customer's language (C§2.12): the title is the label in that language;
    # only the body falls back to the Uzbek template text when the template has none in it
    body, _body_language = render_template(template, language, order_id)
    return _deliver(
        customer,
        dedupe_key=topics.order_status_inbox_key(order_id, status),
        kind=InboxNotification.Kind.ORDER_STATUS,
        title=order_status_title(status, fulfilment, language),
        body=body,
        link={"kind": "order", "order_id": order_id},
        source_type=InboxNotification.SourceType.ORDER,
        source_id=order_id,
        language=language,
        template_status=status,
        data={"type": enums.PushDataType.ORDER_STATUS, "order_id": str(order_id), "status": status},
        topic=topics.PUSH_ORDER_STATUS,
    )


# The appointment statuses under which a ``push.appointment`` action still describes the row.
STILL_TRUE: dict[str, frozenset[str]] = {
    "confirmed": frozenset(OPEN_STATUSES),
    "rescheduled": frozenset(OPEN_STATUSES),
    "rejected": frozenset({enums.AppointmentStatus.REJECTED}),
    "cancelled": frozenset({enums.AppointmentStatus.CANCELLED}),
}


def deliver_appointment(payload: dict[str, Any]) -> InboxNotification | None:
    """``push.appointment {appointment_id, action, event_id, customer_id, reschedule_no}``; an action that no
    longer matches the appointment's status (``STILL_TRUE``) is skipped."""
    action = str(payload["action"])
    if action not in texts.APPOINTMENT_TEXTS:
        raise ValueError(f"No push text for appointment action {action!r}")
    appointment = appointment_selectors.get_appointment(int(payload["appointment_id"]))
    if appointment is None:
        logger.warning("push.appointment for unknown appointment %s", payload["appointment_id"])
        return None
    still_true = STILL_TRUE.get(action)
    if still_true is not None and appointment.status not in still_true:
        # e.g. a staff confirm whose push is delivered after the customer cancelled: the stale notice would be
        # the newest one the customer sees (a customer cancel sends none) — no inbox row, no push
        logger.info(
            "push.appointment skipped: action %s no longer matches appointment %s (%s)",
            action,
            appointment.pk,
            appointment.status,
        )
        return None
    customer = appointment.customer
    reschedule_no = payload.get("reschedule_no")
    clinic = ""
    if action == "rescheduled":
        location = appointment.location
        name = getattr(location, f"name_{customer.language}", "") or location.name_uz
        address = getattr(location, f"address_{customer.language}", "") or location.address_uz
        clinic = ", ".join(part for part in (name, address) if part)
    title, body, language = texts.appointment_text(
        action, customer.language, appointment.start, clinic=clinic
    )
    return _deliver(
        customer,
        dedupe_key=topics.appointment_inbox_key(
            appointment.pk, action, int(reschedule_no) if reschedule_no is not None else None
        ),
        kind=InboxNotification.Kind.APPOINTMENT,
        title=title,
        body=body,
        link={"kind": "appointment", "appointment_id": appointment.pk},
        source_type=InboxNotification.SourceType.APPOINTMENT,
        source_id=appointment.pk,
        language=language,
        data={
            "type": enums.PushDataType.APPOINTMENT,
            "appointment_id": str(appointment.pk),
            "action": action,
        },
        topic=topics.PUSH_APPOINTMENT,
    )


def deliver_account_security(payload: dict[str, Any]) -> InboxNotification | None:
    """Handler body of ``push.account_security`` (C§2.14.2, §2.12): one ``general`` inbox row per
    ``event_id`` (``source_type=account``, ``source_id`` = the customer, ``link {"kind": "none"}``, dedupe
    ``account:{customer_id}:{event}:{event_id}``), the visible notice to every signed-in pushable device
    (B-W1 fan-out), then the same notice (same ``inbox_id``) to each ``farewell_token`` — the devices a reset
    or a password change signed out. The handler blanks those tokens in the stored payload afterwards. No
    PII: the texts carry the provider name only.
    """
    missing = set(account_topics.ACCOUNT_SECURITY_PAYLOAD_KEYS) - set(payload)
    if missing:
        raise ValueError(f"push.account_security payload misses {sorted(missing)}")
    event = str(payload["event"])
    if event not in enums.AccountSecurityEvent.values:
        raise ValueError(f"Unknown account security event {event!r}")
    customer_id = int(payload["customer_id"])
    customer = Customer.objects.filter(pk=customer_id).first()
    if customer is None:
        logger.warning("push.account_security for unknown customer %s", customer_id)
        return None
    provider = str(payload["provider"] or "")
    language = i18n.normalize_language(customer.language)
    brand = push_messages.PROVIDER_BRANDS.get(provider, provider)
    title = i18n.t(f"push.account_security.{event}.title", language)
    body = i18n.t(f"push.account_security.{event}.body", language, provider=brand)
    data = {"type": enums.PushDataType.ACCOUNT_SECURITY, "event": event, "provider": provider}
    inbox = _deliver(
        customer,
        dedupe_key=account_topics.account_security_inbox_key(customer_id, event, str(payload["event_id"])),
        kind=InboxNotification.Kind.GENERAL,
        title=title,
        body=body,
        link={"kind": enums.InboxLinkKind.NONE},
        source_type=InboxNotification.SourceType.ACCOUNT,
        source_id=customer_id,
        language=language,
        data=data,
        topic=account_topics.PUSH_ACCOUNT_SECURITY,
    )
    current = {device.fcm_token for device in selectors.customer_push_devices(customer_id)}
    candidates = list(
        dict.fromkeys(
            str(token)
            for token in payload.get("farewell_tokens") or []
            if token and str(token) not in current
        )
    )
    # C§1.9 step 4 (the push.session_replaced rule): a token now held by an active Device row belongs to an
    # install signed in again since the reset (possibly into another account) — it gets no notice and no
    # inbox id that are not its own.
    held = set(
        Device.objects.filter(fcm_token__in=candidates, is_active=True).values_list("fcm_token", flat=True)
    )
    farewell = [token for token in candidates if token not in held]
    if farewell:
        messages = [
            push_messages.typed_message(token, title, body, {**data, "inbox_id": str(inbox.pk)})
            for token in farewell
        ]
        for token, result in zip(farewell, get_fcm_port().send(messages), strict=True):
            if result.ok:
                continue
            if result.error == UNREGISTERED:
                # a dead token is pruned wherever it still sits (C§2.14.2), as push.session_replaced does
                for device_id in Device.objects.filter(fcm_token=token).values_list("pk", flat=True):
                    account_services.invalidate_fcm_token(device_id, token=token)
            elif result.error == PAYLOAD_INVALID:
                push_messages.log_payload_invalid(account_topics.PUSH_ACCOUNT_SECURITY, inbox.pk)
            else:
                logger.info("push.account_security farewell not delivered error=%s", result.error)
    return inbox


__all__ = [
    "STILL_TRUE",
    "deliver_account_security",
    "deliver_appointment",
    "deliver_order_status",
    "order_status_title",
    "render_template",
]
