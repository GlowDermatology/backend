"""The ops console's full purge of one customer: every row, file and cache key that belongs to them is
deleted, so the phone and the e-mail address can register again as a new customer (``Customer`` and everything
under it gone, unlike ``DELETE me/``, which keeps the row and the phone, decision A1).

Only a customer **without money** may be purged: no order beyond ``awaiting_payment`` / ``cancelled``, no
payment attempt or refund, no shipment or delivery issue, no loyalty ledger entry, hold, purchase or balance.
Those rows are accounting records the provider reconciliations and the reports rely on; such a customer is
refused with the reasons (:class:`PurgeRefused`) before anything is written. A campaign still going out to the
customer alone refuses too (cancel it first).

What goes, in one transaction under the customer's row lock (``FOR UPDATE``: an order or capture inserted for
them meanwhile waits and then fails, it can never slip in after the check):

* orders (unpaid or cancelled) with their items, stock reservations (the stock is free again) and events;
  appointments with their events (the append-only event tables are opened by ``core.db.customer_purge_window``
  for these rows only);
* the loyalty account (zero balance, no entries), the cart, delivery quotes, saved addresses, social
  identities (an Apple grant holding a token is revoked through ``apple.revoke`` first), devices, sessions
  (revoked first: tombstones and QR keys), inbox rows, campaign deliveries, segment memberships, test devices
  and test pushes, the campaigns addressed to the customer alone (with their image files);
* the one-time codes sent to the phone or the e-mail address, with their sealed payloads in Redis; the outbox
  messages about the customer, their codes, sessions, orders, appointments and campaigns (but a pending
  ``apple.revoke``, which still has to reach Apple); the stored Idempotency-Key responses;
* the profile image file (after the commit) and, after the commit, the sign-in guard and per-number OTP
  throttle keys of the phone, the e-mail address and the account, so the next registration starts fresh.

The audit log is kept as it is (owner's decision): its entries about the customer stay, and the console action
adds ``ops.customer_purged`` with the counts below (no phone, e-mail or name). Outside this database the
providers keep their own records (Billz's client card, DevSMS and FCM logs).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Q
from rest_framework.throttling import SimpleRateThrottle

from glowderma.accounts import services as account_services
from glowderma.accounts import topics as account_topics
from glowderma.accounts.models import AuthSession, Customer, CustomerSocialIdentity, Device, SmsCode
from glowderma.accounts.security.identifiers import email_throttle_ident, phone_throttle_ident
from glowderma.appointments.models import Appointment
from glowderma.commerce.models import Order
from glowderma.core import enums
from glowderma.core.db import customer_purge_window
from glowderma.core.models import IdempotencyRecord, OutboxMessage
from glowderma.core.variants import delete_with_variants
from glowderma.customers.services.common import identifiers
from glowderma.delivery.models import DeliveryIssue, Shipment
from glowderma.loyalty.models import CashbackHold, LedgerEntry, LoyaltyAccount, Purchase
from glowderma.notifications import topics as notification_topics
from glowderma.notifications.models import Campaign, CampaignTest
from glowderma.payments.models import Payment

# The order statuses that never took money: an order past ``awaiting_payment`` was paid (or captured).
MONEY_FREE_ORDER_STATUSES = (enums.OrderStatus.AWAITING_PAYMENT, enums.OrderStatus.CANCELLED)

# Why a purge is refused (the console shows a sentence for each).
PAID_ORDERS = "paid_orders"
PAYMENTS = "payments"
DELIVERIES = "deliveries"
LOYALTY = "loyalty"
CAMPAIGN_IN_FLIGHT = "campaign_in_flight"
PURGE_BLOCKERS = (PAID_ORDERS, PAYMENTS, DELIVERIES, LOYALTY, CAMPAIGN_IN_FLIGHT)

# The guard scopes whose keys hold the phone, the e-mail address or the account id.
GUARD_SCOPES = (identifiers.LOGIN_SCOPE, identifiers.REAUTH_SCOPE)
GUARD_KEY_KINDS = ("fail", "attempt", "streak", "lock", "lockn")
# ``accounts.api.throttles.SmsPhoneThrottle.scope``: the per-number budget of ``auth/send-code/``.
SMS_PHONE_THROTTLE_SCOPE = "sms_phone"


class PurgeRefused(Exception):
    """The customer has money (or a campaign in flight): nothing was deleted; ``reasons`` ⊆
    :data:`PURGE_BLOCKERS`."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__(", ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """What was deleted (counts only: they go into the audit row)."""

    orders: int
    appointments: int
    sessions: int
    devices: int
    one_time_codes: int
    outbox_messages: int
    campaigns: int

    def as_dict(self) -> dict[str, int]:
        return {
            "orders": self.orders,
            "appointments": self.appointments,
            "sessions": self.sessions,
            "devices": self.devices,
            "one_time_codes": self.one_time_codes,
            "outbox_messages": self.outbox_messages,
            "campaigns": self.campaigns,
        }


def purge_blockers(customer_id: int) -> list[str]:
    """Why ``customer_id`` cannot be purged (``[]``: it can), in :data:`PURGE_BLOCKERS` order."""
    orders = Order.objects.filter(customer_id=customer_id)
    account = LoyaltyAccount.objects.filter(customer_id=customer_id)
    reasons = []
    if orders.exclude(status__in=MONEY_FREE_ORDER_STATUSES).exists():
        reasons.append(PAID_ORDERS)
    if Payment.objects.filter(order__customer_id=customer_id).exists():
        reasons.append(PAYMENTS)  # every attempt, also a failed one: the provider keeps it for reconciliation
    if (
        Shipment.objects.filter(order__customer_id=customer_id).exists()
        or DeliveryIssue.objects.filter(order__customer_id=customer_id).exists()
    ):
        reasons.append(DELIVERIES)
    if (
        account.exclude(balance=0).exists()
        or LedgerEntry.objects.filter(account__customer_id=customer_id).exists()
        or CashbackHold.objects.filter(account__customer_id=customer_id).exists()
        or Purchase.objects.filter(customer_id=customer_id).exists()
    ):
        reasons.append(LOYALTY)
    if (
        Campaign.objects.filter(audience_customer_id=customer_id)
        .exclude(state__in=Campaign.TERMINAL_STATES | Campaign.EDITABLE_STATES)
        .exists()
    ):
        reasons.append(CAMPAIGN_IN_FLIGHT)
    return reasons


def _delete_append_only(table: str, parent_column: str, parent_ids: list[int]) -> None:
    """Delete the history rows of ``parent_ids`` from an append-only table (plain SQL: the ORM refuses)."""
    if not parent_ids:
        return
    with customer_purge_window(), connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table} WHERE {parent_column} = ANY(%s)",  # noqa: S608 - module constants only
            [parent_ids],
        )


def _delete_file_on_commit(field: object) -> None:
    if not field:
        return
    storage, name = field.storage, str(field.name)  # type: ignore[attr-defined]
    transaction.on_commit(lambda: delete_with_variants(storage, name), robust=True)


def _forget_cache_keys_on_commit(customer_id: int, phone: str | None, email: str | None) -> None:
    """The sign-in guard and the per-number throttle keys of the phone, the e-mail and the account."""
    idents = [ident for ident in (phone_throttle_ident(phone), email_throttle_ident(email)) if ident]
    guard_keys = [*idents, identifiers.customer_guard_key(customer_id)]
    keys = [
        f"auth:{kind}:{scope}:{key}"
        for scope in GUARD_SCOPES
        for key in guard_keys
        for kind in GUARD_KEY_KINDS
    ]
    keys += [
        SimpleRateThrottle.cache_format % {"scope": SMS_PHONE_THROTTLE_SCOPE, "ident": ident}
        for ident in idents
    ]
    transaction.on_commit(lambda: cache.delete_many(keys), robust=True)


def _outbox_rows(
    customer_id: int,
    *,
    code_ids: list[int],
    session_ids: list[str],
    order_ids: list[int],
    appointment_ids: list[int],
    campaign_ids: list[int],
) -> int:
    about = (
        Q(payload__customer_id=customer_id)
        | Q(topic__in=(account_topics.SMS_SEND, account_topics.EMAIL_SEND), payload__sms_code_id__in=code_ids)
        | Q(topic=account_topics.PUSH_SESSION_REPLACED, payload__sid__in=session_ids)
        | Q(payload__order_id__in=order_ids)
        | Q(payload__appointment_id__in=appointment_ids)
        | Q(topic=notification_topics.CAMPAIGN_SEND_BATCH, payload__campaign_id__in=campaign_ids)
    )
    # A revocation not yet done still has to reach Apple (the grant would outlive the purge otherwise).
    unfinished_revocation = Q(topic=account_topics.APPLE_REVOKE) & ~Q(status="done")
    deleted, _ = OutboxMessage.objects.filter(about).exclude(unfinished_revocation).delete()
    return deleted


@transaction.atomic
def purge_customer(customer_id: int) -> PurgeResult:
    """Delete the customer and everything that belongs to them (module doc); :class:`PurgeRefused` when they
    have money (nothing written). ``Customer.DoesNotExist`` when there is no such customer."""
    stored = Customer.objects.select_for_update().get(pk=customer_id)
    reasons = purge_blockers(customer_id)
    if reasons:
        raise PurgeRefused(reasons)
    phone, email = stored.phone, stored.email

    for identity in CustomerSocialIdentity.objects.filter(customer_id=customer_id):
        account_services.enqueue_apple_revoke(identity)
    session_ids = [
        str(sid) for sid in AuthSession.objects.filter(customer_id=customer_id).values_list("pk", flat=True)
    ]
    account_services.revoke_customer_sessions(customer_id, enums.SessionRevokeReason.DEACTIVATED)
    AuthSession.objects.filter(customer_id=customer_id).delete()
    devices = Device.objects.filter(customer_id=customer_id)
    device_ids = list(devices.values_list("pk", flat=True))

    order_ids = list(Order.objects.filter(customer_id=customer_id).values_list("pk", flat=True))
    _delete_append_only("commerce_orderevent", "order_id", order_ids)
    Order.objects.filter(pk__in=order_ids).delete()  # items and stock reservations cascade
    appointment_ids = list(Appointment.objects.filter(customer_id=customer_id).values_list("pk", flat=True))
    _delete_append_only("appointments_appointmentevent", "appointment_id", appointment_ids)
    Appointment.objects.filter(pk__in=appointment_ids).delete()
    LoyaltyAccount.objects.filter(customer_id=customer_id).delete()

    campaigns = list(Campaign.objects.filter(audience_customer_id=customer_id))
    for campaign in campaigns:
        _delete_file_on_commit(campaign.image)
    campaign_ids = [campaign.pk for campaign in campaigns]
    Campaign.objects.filter(pk__in=campaign_ids).delete()  # deliveries and tests cascade
    test_pushes = Q(device_id__in=device_ids)
    if phone:
        test_pushes |= Q(phone=phone)
    CampaignTest.objects.filter(test_pushes).delete()

    sent_to = Q(pk__in=[])
    if phone:
        sent_to |= Q(phone=phone)
    if email:
        sent_to |= Q(email=email)
    code_ids = list(SmsCode.objects.filter(sent_to).values_list("pk", flat=True))
    SmsCode.objects.filter(pk__in=code_ids).delete()
    sealed = [account_services.OTP_PAYLOAD_KEY.format(sms_code_id=pk) for pk in code_ids]
    sealed += [account_services.SOCIAL_OTP_KEY.format(sms_code_id=pk) for pk in code_ids]
    if sealed:
        transaction.on_commit(lambda: cache.delete_many(sealed), robust=True)

    outbox_deleted = _outbox_rows(
        customer_id,
        code_ids=code_ids,
        session_ids=session_ids,
        order_ids=order_ids,
        appointment_ids=appointment_ids,
        campaign_ids=campaign_ids,
    )
    IdempotencyRecord.objects.filter(audience="customer", actor_id=customer_id).delete()

    _delete_file_on_commit(stored.image)
    # addresses, social identities, devices (their test devices), the cart, delivery quotes, inbox rows,
    # campaign deliveries and segment memberships cascade
    Customer.objects.filter(pk=customer_id).delete()
    _forget_cache_keys_on_commit(customer_id, phone, email)
    return PurgeResult(
        orders=len(order_ids),
        appointments=len(appointment_ids),
        sessions=len(session_ids),
        devices=len(device_ids),
        one_time_codes=len(code_ids),
        outbox_messages=outbox_deleted,
        campaigns=len(campaign_ids),
    )


__all__ = [
    "CAMPAIGN_IN_FLIGHT",
    "DELIVERIES",
    "LOYALTY",
    "MONEY_FREE_ORDER_STATUSES",
    "PAID_ORDERS",
    "PAYMENTS",
    "PURGE_BLOCKERS",
    "PurgeRefused",
    "PurgeResult",
    "purge_blockers",
    "purge_customer",
]
