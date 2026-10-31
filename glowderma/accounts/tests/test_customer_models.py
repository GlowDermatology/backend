"""Customer, Device, Address, SmsCode, CustomerSocialIdentity and the AuthSession changes (design 01 §1, §8).

Every CHECK / partial-unique constraint is exercised through ``IntegrityError`` on a raw ``objects.create``;
every index is asserted in ``pg_catalog``.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from glowderma.accounts.models import Address, AuthSession, Customer, Device, SmsCode
from glowderma.accounts.tests.factories import (
    AddressFactory,
    AuthSessionFactory,
    CustomerFactory,
    CustomerSessionFactory,
    CustomerSocialIdentityFactory,
    DeviceFactory,
    SmsCodeFactory,
    StaffUserFactory,
)
from glowderma.core import enums, timeutils

pytestmark = pytest.mark.django_db


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def constraint_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype IN ('c', 'u')",
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


def create_fails(model: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.create(**kwargs)


# --- Customer ------------------------------------------------------------


def test_customer_defaults_and_str() -> None:
    customer = CustomerFactory.create(phone="+998901234567", name="Malika Karimova")
    assert customer.language == enums.ContentLanguage.UZ
    assert customer.status == enums.CustomerStatus.ACTIVE
    assert customer.registered_at is not None and customer.deleted_at is None
    assert customer.password == "" and customer.address_text == ""
    assert str(customer) == "Malika Karimova · +998 90 *** ** 67"  # the phone masked (contract §1.4)
    assert str(CustomerFactory.create(phone="+998901234568", name="")) == "+998 90 *** ** 68"


@pytest.mark.parametrize("phone", ["998901234567", "+99890123456", "+998901234a67", "+790012345678", ""])
def test_customer_phone_must_be_uzbek_e164(phone: str) -> None:
    create_fails(Customer, phone=phone, name="x")


def test_customer_phone_is_unique() -> None:
    CustomerFactory.create(phone="+998901234567")
    create_fails(Customer, phone="+998901234567", name="again")


def test_customer_status_and_deleted_at_are_consistent() -> None:
    create_fails(Customer, phone="+998901234561", status=enums.CustomerStatus.DELETED)
    create_fails(
        Customer, phone="+998901234562", status=enums.CustomerStatus.ACTIVE, deleted_at=timeutils.now()
    )
    Customer.objects.create(
        phone="+998901234563", status=enums.CustomerStatus.DELETED, deleted_at=timeutils.now()
    )


def test_customer_consent_requires_terms_version() -> None:
    create_fails(Customer, phone="+998901234561", consent_at=timeutils.now())
    Customer.objects.create(phone="+998901234562", consent_at=timeutils.now(), terms_version="20261024-1")


def test_customer_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("accounts_customer")
    trigram = indexes["accounts_customer_fold_trgm"]  # accounts.0008: the folded name (GC-1)
    assert "USING gin" in trigram and "gin_trgm_ops" in trigram and "translate(lower(" in trigram.lower()
    assert "accounts_customer_name_trgm" not in indexes
    assert "status" in indexes["accounts_customer_status_reg"]
    assert "registered_at" in indexes["accounts_customer_status_reg"]
    assert constraint_names("accounts_customer") >= {
        "customer_phone_e164",
        "customer_status_deleted_at_consistent",
        "customer_consent_has_terms_version",
    }


def test_customer_default_ordering_is_newest_registration_first() -> None:
    now = timeutils.now()
    old = CustomerFactory.create(registered_at=now - timedelta(days=2))
    new = CustomerFactory.create(registered_at=now - timedelta(days=1))
    same = CustomerFactory.create(registered_at=now - timedelta(days=1))
    assert list(Customer.objects.all()) == [same, new, old]


@pytest.mark.parametrize("term", ["KARIM", "g'ulom", "Gʻulom"])
def test_customer_name_search_uses_the_trigram_index(term: str) -> None:
    """``fold(name) LIKE %…%`` is the search shape the index serves (01 §1.1 contract notes; folded since
    accounts.0008, decision GC-1)."""
    from django.db.models import F, Value
    from django.db.models.lookups import Contains

    from glowderma.core.search import SearchFold

    CustomerFactory.create(name="Malika Karimova")
    CustomerFactory.create(name="Dilnoza G\u2018ulomova")
    CustomerFactory.create(name="Sherzod Qodirov")
    queryset = Customer.objects.filter(Contains(SearchFold(F("name")), SearchFold(Value(term))))
    assert len(queryset) == 1
    with connection.cursor() as cursor:
        # two rows: with a sequential scan ruled out the planner may still prefer walking the
        # ``registered_at`` ordering index with a filter; rule plain index scans out as well so that the
        # only remaining access path is the bitmap scan the trigram GIN index serves
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("SET LOCAL enable_indexscan = off")
        cursor.execute("SET LOCAL enable_indexonlyscan = off")
        sql, params = queryset.query.sql_with_params()
        cursor.execute("EXPLAIN " + sql, params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "accounts_customer_fold_trgm" in plan


# --- Device ------------------------------------------------------------


def test_device_str_and_defaults() -> None:
    device = DeviceFactory.create(model="iPhone 14")
    assert device.is_active and device.push_enabled and device.locale == ""
    assert str(device) == f"iOS iPhone 14 · mijoz #{device.customer_id}"
    bare = DeviceFactory.create(model="", fid="abc", platform=Device.Platform.ANDROID)
    assert str(bare) == f"Android abc · mijoz #{bare.customer_id}"


def test_device_fid_unique_per_customer() -> None:
    device = DeviceFactory.create(fid="same")
    create_fails(Device, customer=device.customer, fid="same", platform="ios")
    DeviceFactory.create(fid="same")  # another customer may carry the same installation id


def test_device_active_fcm_token_unique_only_while_active_and_non_empty() -> None:
    DeviceFactory.create(fcm_token="tok-1")  # noqa: S106 - device token, not a password
    create_fails(Device, customer=CustomerFactory.create(), fid="x", platform="ios", fcm_token="tok-1")  # noqa: S106
    DeviceFactory.create(fcm_token="tok-1", is_active=False)  # noqa: S106 - inactive duplicate allowed
    DeviceFactory.create(fcm_token="", push_enabled=False)
    DeviceFactory.create(fcm_token="", push_enabled=False)  # empty tokens never collide


def test_device_push_requires_a_token() -> None:
    create_fails(
        Device, customer=CustomerFactory.create(), fid="x", platform="ios", fcm_token="", push_enabled=True
    )
    DeviceFactory.create(fcm_token="", push_enabled=False)


def test_device_indexes_in_pg_catalog() -> None:
    indexes = index_definitions("accounts_device")
    assert "customer_id" in indexes["accounts_device_cust_active"]
    assert "last_active_at" in indexes["accounts_device_cust_active"]
    assert "WHERE" in indexes["accounts_device_pushable"]
    assert "WHERE" in indexes["device_active_fcm_token_unique"]
    assert constraint_names("accounts_device") >= {"device_customer_fid_unique", "device_push_requires_token"}


def test_device_cascades_from_customer_delete() -> None:
    device = DeviceFactory.create()
    device.customer.delete()  # theoretical: customers are soft-deleted; the cascade must still be safe
    assert not Device.objects.filter(pk=device.pk).exists()


# --- Address ------------------------------------------------------------


def test_address_one_default_per_customer() -> None:
    address = AddressFactory.create(is_default=True)
    create_fails(Address, customer=address.customer, text="second", is_default=True)
    AddressFactory.create(customer=address.customer, is_default=False)
    AddressFactory.create(is_default=True)  # a different customer


def test_address_latitude_and_longitude_come_together() -> None:
    customer = CustomerFactory.create()
    create_fails(Address, customer=customer, text="x", latitude=Decimal("41.311081"))
    create_fails(Address, customer=customer, text="x", longitude=Decimal("69.240562"))
    AddressFactory.create(customer=customer, latitude=Decimal("41.311081"), longitude=Decimal("69.240562"))


def test_address_ordering_and_str() -> None:
    customer = CustomerFactory.create()
    older = AddressFactory.create(customer=customer, label="")
    default = AddressFactory.create(customer=customer, is_default=True, label="Uy")
    newest = AddressFactory.create(customer=customer)
    assert list(Address.objects.filter(customer=customer)) == [default, newest, older]
    assert str(default) == f"Uy #{default.pk} · mijoz #{customer.pk}"
    assert str(older) == f"Manzil #{older.pk} · mijoz #{customer.pk}"


# --- SmsCode ------------------------------------------------------------


def test_smscode_phone_and_attempts_checks() -> None:
    create_fails(SmsCode, phone="901234567", purpose="signup", code_hmac="a" * 64, expires_at=timeutils.now())
    create_fails(
        SmsCode,
        phone="+998901234567",
        purpose="signup",
        code_hmac="a" * 64,
        expires_at=timeutils.now(),
        attempts=6,
        max_attempts=5,
    )
    code = SmsCodeFactory.create(attempts=5, max_attempts=5)
    assert code.sent_at is None and code.consumed_at is None


def test_smscode_verification_token_unique_when_set() -> None:
    SmsCodeFactory.create(verification_token_hash="h" * 64)
    with pytest.raises(IntegrityError), transaction.atomic():
        SmsCodeFactory.create(verification_token_hash="h" * 64)
    SmsCodeFactory.create(verification_token_hash="")
    SmsCodeFactory.create(verification_token_hash="")


def test_smscode_indexes_and_str() -> None:
    indexes = index_definitions("accounts_smscode")
    assert "purpose" in indexes["accounts_smscode_phone_purpose"]
    assert "expires_at" in indexes["accounts_smscode_expires"]
    assert "WHERE" in indexes["smscode_verification_token_unique"]
    code = SmsCodeFactory.create(phone="+998901234567", purpose=SmsCode.Purpose.RESET)
    assert str(code).startswith("Parolni tiklash kodi · +998 90 *** ** 67 · ")


# --- CustomerSocialIdentity ------------------------------------------------------------


def test_customer_social_identity_uniques_and_str() -> None:
    identity = CustomerSocialIdentityFactory.create(provider="google", subject="s-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomerSocialIdentityFactory.create(provider="google", subject="s-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomerSocialIdentityFactory.create(customer=identity.customer, provider="google", subject="s-2")
    CustomerSocialIdentityFactory.create(customer=identity.customer, provider="facebook", subject="s-1")
    assert str(identity) == f"Google: s-1 → mijoz #{identity.customer_id}"


def test_staff_and_customer_social_identities_may_share_a_subject() -> None:
    from glowderma.accounts.models import SocialIdentity

    SocialIdentity.objects.create(staff=StaffUserFactory.create(), provider="google", subject="shared")
    CustomerSocialIdentityFactory.create(provider="google", subject="shared")


# --- AuthSession ------------------------------------------------------------


def test_staff_session_unchanged_by_the_customer_columns(staff_session: tuple[AuthSession, str]) -> None:
    session, _ = staff_session
    session.refresh_from_db()
    assert session.customer is None and session.device is None
    assert session.previous_refresh_hash == "" and session.rotated_at is None


def test_customer_session_round_trip() -> None:
    device = DeviceFactory.create()
    session = CustomerSessionFactory.create(customer=device.customer, device=device)
    assert device.customer.sessions.get() == session
    assert device.sessions.get() == session
    device.delete()
    session.refresh_from_db()
    assert session.device is None  # SET_NULL


def test_authsession_subject_must_match_audience() -> None:
    customer = CustomerFactory.create()
    staff = StaffUserFactory.create()
    with pytest.raises(IntegrityError), transaction.atomic():
        AuthSessionFactory.create(staff=staff, customer=customer)
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomerSessionFactory.create(customer=customer, staff=staff)
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomerSessionFactory.create(customer=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        AuthSession.objects.create(
            audience="staff", device_label="x", refresh_hash="r" * 64, absolute_expires_at=timeutils.now()
        )


def test_authsession_customer_fk_enforces_existence() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        AuthSession.objects.create(
            audience="customer",
            customer_id=987654321,
            device_label="x",
            refresh_hash="r" * 64,
            absolute_expires_at=timeutils.now(),
        )
        connection.check_constraints()  # the FK is DEFERRABLE INITIALLY DEFERRED


def test_authsession_customer_is_protected() -> None:
    from django.db.models import ProtectedError

    customer = CustomerFactory.create()
    CustomerSessionFactory.create(customer=customer)
    with pytest.raises(ProtectedError):
        customer.delete()


def test_authsession_indexes_in_pg_catalog() -> None:
    indexes = index_definitions("accounts_authsession")
    assert "customer_id" in indexes["accounts_authsession_cust_rev"]
    assert "staff_id" in indexes["accounts_authsession_staff_rev"]
    assert "authsession_subject_matches_audience" in constraint_names("accounts_authsession")
