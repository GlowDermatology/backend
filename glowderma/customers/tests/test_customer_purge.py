# ruff: noqa: S105, S106 - test credentials
"""The ops console's full purge of a customer (``customers.services.purge_customer``, the customer page's
«To‘liq o‘chirish» action): a customer without money goes with every row, file and cache key that belongs to
them, and the phone registers again as a brand-new customer; a customer with money is refused with nothing
deleted."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from django.apps import apps
from django.core.cache import cache
from django.db import DatabaseError, connection, transaction
from django.urls import reverse

from glowderma.accounts import services as account_services
from glowderma.accounts.models import AuthSession, Customer, SmsCode
from glowderma.accounts.tests.factories import (
    AddressFactory,
    CustomerFactory,
    CustomerSessionFactory,
    CustomerSocialIdentityFactory,
    DeviceFactory,
    SignedInDeviceFactory,
    SmsCodeFactory,
)
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.appointments.tests.factories import AppointmentEventFactory, AppointmentFactory
from glowderma.commerce.models import Order, OrderEvent, StockReservation
from glowderma.commerce.tests.factories import (
    CartFactory,
    CartItemFactory,
    OrderEventFactory,
    OrderFactory,
    OrderItemFactory,
    StockReservationFactory,
)
from glowderma.core import enums, timeutils
from glowderma.core.db import CUSTOMER_PURGE_TABLES, PURGEABLE_TABLES, customer_purge_window
from glowderma.core.models import AuditEntry, IdempotencyRecord, OutboxMessage
from glowderma.core.tests.console import confirm, console_client, denied, messages_of, user_of
from glowderma.customers import services
from glowderma.customers.admin import PURGE_REFUSALS
from glowderma.customers.tests.p1_support import (
    PASSWORD,
    TERMS,
    app_client,
    publish_terms,
    reset_fakes,
    sent_codes,
)
from glowderma.delivery.tests.factories import DeliveryIssueFactory, DeliveryQuoteFactory, ShipmentFactory
from glowderma.loyalty.tests.factories import (
    CashbackHoldFactory,
    LedgerEntryFactory,
    LoyaltyAccountFactory,
    PurchaseFactory,
)
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CampaignRecipientFactory,
    CampaignTestFactory,
    CustomerSegmentFactory,
    InboxNotificationFactory,
    SegmentMemberFactory,
    SendingCampaignFactory,
    TestDeviceFactory,
)
from glowderma.payments.tests.factories import PaymentFactory

pytestmark = pytest.mark.django_db

PHONE = "+998907771122"
EMAIL = "malika@example.uz"
SEND = "/api/v1/auth/send-code/"
VERIFY = "/api/v1/auth/verify-code/"
SET_PASSWORD = "/api/v1/auth/set-password/"

# Every relation that points at a customer, and how the purge deals with it. A new one fails this list: decide
# whether it is money (a blocker) or the customer's own data (deleted), then add it here.
CUSTOMER_RELATIONS = {
    "accounts.AuthSession.customer",
    "accounts.Address.customer",
    "accounts.CustomerSocialIdentity.customer",
    "accounts.Device.customer",
    "loyalty.LoyaltyAccount.customer",
    "loyalty.Purchase.customer",
    "delivery.DeliveryQuote.customer",
    "commerce.Cart.customer",
    "commerce.Order.customer",
    "appointments.Appointment.customer",
    "notifications.SegmentMember.customer",
    "notifications.Campaign.audience_customer",
    "notifications.CampaignRecipient.customer",
    "notifications.InboxNotification.customer",
}
# The columns outside those relations that copy the phone or the e-mail address.
CONTACT_COLUMNS = (
    ("accounts", "SmsCode", "phone"),
    ("accounts", "SmsCode", "email"),
    ("commerce", "Order", "recipient_phone"),
    ("notifications", "Campaign", "audience_phone"),
    ("notifications", "TestDevice", "phone"),
    ("notifications", "CampaignTest", "phone"),
)


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def purge_url(customer_id: int) -> str:
    return reverse("admin:accounts_customer_purge_customer", args=[customer_id])


def relations_to_customer() -> list[Any]:
    return [
        field
        for field in Customer._meta.get_fields(include_hidden=True)
        if field.is_relation and field.auto_created and not field.concrete
    ]


def rich_customer() -> Customer:
    """A customer without money with a row in every table the purge clears."""
    customer = CustomerFactory.create(
        phone=PHONE, email=EMAIL, email_verified_at=timeutils.now(), name="Malika Karimova"
    )
    AddressFactory.create(customer=customer)
    CustomerSocialIdentityFactory.create(customer=customer)
    device = SignedInDeviceFactory.create(customer=customer)
    CustomerSessionFactory.create(customer=customer)
    LoyaltyAccountFactory.create(customer=customer)
    cart = CartFactory.create(customer=customer)
    CartItemFactory.create(cart=cart)
    DeliveryQuoteFactory.create(customer=customer)
    unpaid = OrderFactory.create(customer=customer, recipient_phone=PHONE)
    OrderItemFactory.create(order=unpaid)
    StockReservationFactory.create(order=unpaid)
    OrderEventFactory.create(order=unpaid)
    cancelled = OrderFactory.create(customer=customer, cancelled=True)
    OrderEventFactory.create(order=cancelled)
    visit = AppointmentFactory.create(customer=customer)
    AppointmentEventFactory.create(appointment=visit)
    SegmentMemberFactory.create(customer=customer, segment=CustomerSegmentFactory.create())
    InboxNotificationFactory.create(customer=customer)
    CampaignRecipientFactory.create(customer=customer, device=device)
    test_device = TestDeviceFactory.create(device=device)
    CampaignTestFactory.create(test_device=test_device)
    CampaignFactory.create(
        audience_type=enums.AudienceType.CUSTOMER, audience_customer=customer, audience_phone=PHONE
    )
    code = SmsCodeFactory.create(phone=PHONE)
    SmsCodeFactory.create(phone="", email=EMAIL, purpose=SmsCode.Purpose.VERIFY_EMAIL)
    cache.set(account_services.otp_payload_key(code.pk), "sealed", 120)
    OutboxMessage.objects.create(
        topic="push.account_security", payload={"customer_id": customer.pk}, dedupe_key="purge-1"
    )
    OutboxMessage.objects.create(topic="sms.send", payload={"sms_code_id": code.pk}, dedupe_key="purge-2")
    OutboxMessage.objects.create(
        topic="push.order_status", payload={"order_id": unpaid.pk}, dedupe_key="purge-3"
    )
    IdempotencyRecord.objects.create(
        audience="customer",
        actor_id=customer.pk,
        key=uuid.uuid4(),
        method="PATCH",
        path="/api/v1/me/",
        request_hash="0" * 64,
        response_status=200,
        response_body={"phone": PHONE},
    )
    return customer


def assert_nothing_left(customer_id: int) -> None:
    assert not Customer.objects.filter(pk=customer_id).exists()
    for field in relations_to_customer():
        model = field.related_model
        assert not model._base_manager.filter(**{f"{field.field.name}_id": customer_id}).exists(), field
    for app_label, model_name, column in CONTACT_COLUMNS:
        model = apps.get_model(app_label, model_name)
        for value in (PHONE, EMAIL):
            assert not model._base_manager.filter(**{column: value}).exists(), (model_name, column)
    assert not OutboxMessage.objects.filter(payload__customer_id=customer_id).exists()
    assert not IdempotencyRecord.objects.filter(audience="customer", actor_id=customer_id).exists()


# --- the service -------------------------------------------------------------------------------------------


def test_every_relation_to_a_customer_is_handled_by_the_purge() -> None:
    found = {f"{field.related_model._meta.label}.{field.field.name}" for field in relations_to_customer()}
    assert found == CUSTOMER_RELATIONS


def test_purge_deletes_everything_of_a_customer_without_money(
    django_capture_on_commit_callbacks: Any,
) -> None:
    customer = rich_customer()
    other = CustomerFactory.create()
    other_order = OrderFactory.create(customer=other)
    OrderEventFactory.create(order=other_order)
    code_id = SmsCode.objects.get(phone=PHONE).pk
    with django_capture_on_commit_callbacks(execute=True):
        result = services.purge_customer(customer.pk)
    assert result.as_dict() == {
        "orders": 2,
        "appointments": 1,
        "sessions": 2,
        "devices": 1,
        "one_time_codes": 2,
        "outbox_messages": 3,
        "campaigns": 1,
    }
    assert_nothing_left(customer.pk)
    assert not Order.objects.filter(customer_id=customer.pk).exists()
    assert not StockReservation.objects.filter(order__customer_id=customer.pk).exists()
    assert not Appointment.objects.filter(customer_id=customer.pk).exists()
    assert not AppointmentEvent.objects.exists()
    assert cache.get(account_services.otp_payload_key(code_id)) is None
    # nobody else's rows are touched
    assert Customer.objects.filter(pk=other.pk).exists()
    assert OrderEvent.objects.filter(order=other_order).count() == 1


def test_a_pending_apple_revocation_survives_the_purge() -> None:
    customer = CustomerFactory.create()
    CustomerSocialIdentityFactory.create(
        customer=customer, provider="apple", provider_refresh_token="sealed-token", subject="apple-sub"
    )
    services.purge_customer(customer.pk)
    [message] = OutboxMessage.objects.filter(topic="apple.revoke")
    assert message.status == "pending" and message.payload["sealed_token"] == "sealed-token"
    assert not Customer.objects.filter(pk=customer.pk).exists()


def test_sessions_are_revoked_before_they_go() -> None:
    customer = CustomerFactory.create()
    session = CustomerSessionFactory.create(customer=customer)
    services.purge_customer(customer.pk)
    assert not AuthSession.objects.filter(pk=session.pk).exists()


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda c: OrderFactory.create(customer=c, paid=True), "paid_orders"),
        (lambda c: PaymentFactory.create(order=OrderFactory.create(customer=c)), "payments"),
        (
            lambda c: PaymentFactory.create(
                order=OrderFactory.create(customer=c, cancelled=True), failed=True
            ),
            "payments",
        ),
        (lambda c: ShipmentFactory.create(order__customer=c), "deliveries"),
        (lambda c: DeliveryIssueFactory.create(order__customer=c), "deliveries"),
        (lambda c: LoyaltyAccountFactory.create(customer=c, balance=5_000), "loyalty"),
        (lambda c: LedgerEntryFactory.create(account=LoyaltyAccountFactory.create(customer=c)), "loyalty"),
        (lambda c: CashbackHoldFactory.create(account=LoyaltyAccountFactory.create(customer=c)), "loyalty"),
        (lambda c: PurchaseFactory.create(customer=c, store=True), "loyalty"),
        (
            lambda c: SendingCampaignFactory.create(
                audience_type=enums.AudienceType.CUSTOMER, audience_customer=c, audience_phone=c.phone
            ),
            "campaign_in_flight",
        ),
    ],
)
def test_a_customer_with_money_is_refused_and_nothing_is_deleted(make: Any, reason: str) -> None:
    customer = CustomerFactory.create()
    AddressFactory.create(customer=customer)
    make(customer)
    assert reason in services.purge_blockers(customer.pk)
    with pytest.raises(services.PurgeRefused) as refusal:
        services.purge_customer(customer.pk)
    assert reason in refusal.value.reasons
    assert Customer.objects.filter(pk=customer.pk).exists()
    assert customer.addresses.count() == 1


def test_every_refusal_has_a_console_sentence() -> None:
    assert set(PURGE_REFUSALS) == set(services.PURGE_BLOCKERS)


def test_the_customer_purge_window_opens_only_the_two_histories() -> None:
    assert CUSTOMER_PURGE_TABLES == ("commerce_orderevent", "appointments_appointmentevent")
    assert not set(CUSTOMER_PURGE_TABLES) & set(PURGEABLE_TABLES)
    event = OrderEventFactory.create()
    with (
        pytest.raises(DatabaseError, match="append-only"),
        customer_purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE commerce_orderevent SET data = '{}' WHERE id = %s", [event.pk])
    entry = AuditEntry.objects.create(action="x.y", entity_type="x", entity_id="1", actor_type="system")
    with (
        pytest.raises(DatabaseError, match="append-only"),
        customer_purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM core_auditentry WHERE id = %s", [entry.pk])
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM commerce_orderevent WHERE id = %s", [event.pk])


# --- registering again --------------------------------------------------------------------------------------


def register(phone: str, capture: Any) -> Any:
    client = app_client()
    with capture(execute=True):  # the sms.send handler runs on commit
        assert client.post(SEND, {"phone": phone, "purpose": "register"}).status_code == 200
    verified = client.post(VERIFY, {"phone": phone, "purpose": "register", "code": sent_codes()[-1]})
    assert verified.status_code == 200, verified.data
    body = {
        "verification_token": verified.data["verification_token"],
        "password": PASSWORD,
        "name": "Malika",
        "terms_version": TERMS,
    }
    with capture(execute=True):
        return client.post(SET_PASSWORD, body)


def test_the_phone_registers_again_as_a_new_customer(django_capture_on_commit_callbacks: Any) -> None:
    publish_terms()
    first = register(PHONE, django_capture_on_commit_callbacks)
    assert first.status_code == 201, first.data
    old_id = first.data["customer"]["id"]
    assert SmsCode.objects.filter(phone=PHONE).exists()
    with django_capture_on_commit_callbacks(execute=True):
        services.purge_customer(old_id)
    assert_nothing_left(old_id)
    again = register(PHONE, django_capture_on_commit_callbacks)
    assert again.status_code == 201, again.data
    assert again.data["is_new_customer"] is True and again.data["reactivated"] is False
    fresh = Customer.objects.get(phone=PHONE)
    assert fresh.pk != old_id and fresh.reactivated_at is None and fresh.status == "active"


# --- the console action -------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["anon", "customer_like", "staff", "manager"])
def test_only_superusers_may_purge(role: str) -> None:
    customer = CustomerFactory.create()
    response = confirm(console_client(role), purge_url(customer.pk), identifier=customer.phone)
    assert denied(response)
    assert Customer.objects.filter(pk=customer.pk).exists()


def test_the_button_is_shown_to_superusers_only() -> None:
    customer = CustomerFactory.create()
    change = reverse("admin:accounts_customer_change", args=[customer.pk])
    assert purge_url(customer.pk) in console_client("superuser").get(change).content.decode()
    assert purge_url(customer.pk) not in console_client("manager").get(change).content.decode()


def test_the_confirm_step_needs_the_customer_s_own_phone() -> None:
    customer = CustomerFactory.create(phone=PHONE)
    client = console_client("superuser")
    page = client.get(purge_url(customer.pk))
    assert page.status_code == 200 and "Mijozni to‘liq o‘chirish" in page.content.decode()
    wrong = confirm(client, purge_url(customer.pk), identifier="+998901234567")
    assert wrong.status_code == 200 and "Bu mijozning telefon raqami" in wrong.content.decode()
    assert confirm(client, purge_url(customer.pk)).status_code == 200  # nothing typed
    assert Customer.objects.filter(pk=customer.pk).exists()


@pytest.mark.parametrize(
    ("fields", "typed"),
    [
        ({"phone": PHONE}, "+998 90 777 11 22"),
        ({"phone": None, "email": EMAIL, "email_verified_at": timeutils.now()}, "Malika@Example.uz"),
        ({"phone": None, "email": None}, "#{pk}"),
    ],
)
def test_a_superuser_purges_and_lands_on_the_list(fields: dict[str, Any], typed: str) -> None:
    customer = CustomerFactory.create(**fields)
    DeviceFactory.create(customer=customer)
    client = console_client("superuser")
    response = confirm(client, purge_url(customer.pk), identifier=typed.format(pk=customer.pk))
    assert response.status_code == 302
    assert response["Location"] == reverse("admin:accounts_customer_changelist")
    assert messages_of(response) == [("success", f"Mijoz #{customer.pk} butunlay o‘chirildi.")]
    assert not Customer.objects.filter(pk=customer.pk).exists()
    entry = AuditEntry.objects.get(action="ops.customer_purged")
    assert (entry.actor_id, entry.entity_id) == (user_of(client).pk, str(customer.pk))
    assert entry.after is not None and entry.after["devices"] == 1
    assert PHONE not in str(entry.after) and EMAIL not in str(entry.after)


def test_a_customer_with_money_has_no_button_and_a_direct_request_is_refused() -> None:
    customer = CustomerFactory.create()
    OrderFactory.create(customer=customer, paid=True)
    client = console_client("superuser")
    change = reverse("admin:accounts_customer_change", args=[customer.pk])
    assert purge_url(customer.pk) not in client.get(change).content.decode()
    response = confirm(client, purge_url(customer.pk), identifier=customer.phone)
    assert response.status_code == 302
    [(level, text)] = messages_of(response)
    assert level == "warning" and "to‘langan buyurtmalari bor" in text
    assert Customer.objects.filter(pk=customer.pk).exists()
    assert not AuditEntry.objects.filter(action="ops.customer_purged").exists()
