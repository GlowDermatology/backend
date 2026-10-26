"""factory-boy factories for the accounts module.

``TEST_PASSWORD`` is the one shared password every factory-made staff user can log in with; tests
that need the raw refresh token of a factory-made session read it with :func:`raw_refresh`.
Call the factories as ``StaffUserFactory.create(...)``: the typed ``create`` returns the model.
"""

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from factory.base import Factory
from factory.declarations import LazyFunction, Sequence, SubFactory
from factory.django import DjangoModelFactory
from factory.helpers import post_generation

from glowderma.accounts import services
from glowderma.accounts.models import (
    Address,
    AuthSession,
    Customer,
    CustomerSocialIdentity,
    Device,
    SmsCode,
    StaffUser,
)
from glowderma.core import timeutils

TEST_PASSWORD = "Saffron!Harbor-Long72"  # noqa: S105

# Keyed by primary key, so the token stays readable for any instance of that session (a re-fetched
# row, a ``refresh_from_db`` copy) regardless of whether the original object is still alive.
_refresh_tokens: dict[uuid.UUID, str] = {}


class StaffUserFactory(DjangoModelFactory[StaffUser]):
    class Meta:
        model = StaffUser
        skip_postgeneration_save = True

    email = Sequence(lambda n: f"staff{n + 1}@x.uz")  # type: ignore[no-untyped-call]
    full_name = Sequence(lambda n: f"Staff User {n + 1}")  # type: ignore[no-untyped-call]
    is_active = True

    @post_generation  # type: ignore[untyped-decorator]
    def password(obj: StaffUser, create: bool, extracted: str | None, **kwargs: Any) -> None:
        """Hash ``password=`` (default ``TEST_PASSWORD``) after the row exists, then persist it."""
        obj.set_password(TEST_PASSWORD if extracted is None else extracted)
        if create:
            obj.save(update_fields=["password"])


class AuthSessionFactory(Factory[AuthSession]):
    """A staff session started through ``services.start_staff_session`` (never a raw ``create``).

    ``staff`` and ``device_label`` go to the service; every other kwarg (``revoked_at``,
    ``absolute_expires_at``, ``last_used_at``, ...) is written onto the row afterwards, so overrides
    are honoured and an unknown field name raises ``FieldDoesNotExist`` instead of being dropped.
    """

    class Meta:
        model = AuthSession

    staff = SubFactory(StaffUserFactory)  # type: ignore[no-untyped-call]
    device_label = "pytest"

    @classmethod
    def _create(cls, model_class: type[AuthSession], *args: Any, **kwargs: Any) -> AuthSession:
        staff = kwargs.pop("staff")
        device_label = kwargs.pop("device_label")
        for name in kwargs:
            model_class._meta.get_field(name)  # unknown names raise FieldDoesNotExist before any write
        session, refresh = services.start_staff_session(staff, device_label=device_label)
        if kwargs:
            model_class.objects.filter(pk=session.pk).update(**kwargs)
            session.refresh_from_db()
        _refresh_tokens[session.pk] = refresh
        return session


def raw_refresh(session: AuthSession) -> str:
    """The raw refresh token of a session made by ``AuthSessionFactory`` (any instance with that pk)."""
    return _refresh_tokens[session.pk]


class CustomerFactory(DjangoModelFactory[Customer]):
    class Meta:
        model = Customer

    phone = Sequence(lambda n: f"+99890{n + 1:07d}")  # type: ignore[no-untyped-call]
    name = Sequence(lambda n: f"Mijoz {n + 1}")  # type: ignore[no-untyped-call]


class DeviceFactory(DjangoModelFactory[Device]):
    class Meta:
        model = Device

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    fid = Sequence(lambda n: f"fid-{n + 1}")  # type: ignore[no-untyped-call]
    platform = Device.Platform.IOS
    model = "iPhone 14"
    os = "iOS 18.1"
    app_version = "1.0.0"
    fcm_token = Sequence(lambda n: f"fcm-{n + 1}")  # type: ignore[no-untyped-call]
    push_enabled = True


class AddressFactory(DjangoModelFactory[Address]):
    class Meta:
        model = Address

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    label = "Uy"
    text = Sequence(lambda n: f"Toshkent, Chilonzor, {n + 1}-kvartal")  # type: ignore[no-untyped-call]


def _expires_at() -> datetime:
    return timeutils.now() + timedelta(seconds=120)


class SmsCodeFactory(DjangoModelFactory[SmsCode]):
    class Meta:
        model = SmsCode

    phone = Sequence(lambda n: f"+99891{n + 1:07d}")  # type: ignore[no-untyped-call]
    purpose = SmsCode.Purpose.SIGNUP
    code_hmac = Sequence(lambda n: hashlib.sha256(f"code-{n}".encode()).hexdigest())  # type: ignore[no-untyped-call]
    expires_at = LazyFunction(_expires_at)  # type: ignore[no-untyped-call]


class CustomerSocialIdentityFactory(DjangoModelFactory[CustomerSocialIdentity]):
    class Meta:
        model = CustomerSocialIdentity

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    provider = "google"
    subject = Sequence(lambda n: f"sub-{n + 1}")  # type: ignore[no-untyped-call]
    email_at_link = Sequence(lambda n: f"customer{n + 1}@gmail.com")  # type: ignore[no-untyped-call]


class CustomerSessionFactory(DjangoModelFactory[AuthSession]):
    """A customer-audience session written directly (the M2 service does not exist yet)."""

    class Meta:
        model = AuthSession

    audience = "customer"
    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    device_label = "pytest"
    refresh_hash = Sequence(lambda n: hashlib.sha256(f"customer-refresh-{n}".encode()).hexdigest())  # type: ignore[no-untyped-call]
    absolute_expires_at = LazyFunction(lambda: timeutils.now() + timedelta(days=30))  # type: ignore[no-untyped-call]


# --- M2 customer sessions (plan §1 P0) ----------------------------------------------------------------------

TEST_FID = "fid-test-0001"
TEST_PLATFORM = "ios"
TEST_APP_VERSION = "1.0.0"

# Raw device keys issued by :func:`start_test_session`, keyed by device pk (the server stores only the hash).
_device_keys: dict[int, str] = {}


def start_test_session(
    customer: Customer,
    *,
    fid: str = TEST_FID,
    platform: str = TEST_PLATFORM,
    app_version: str = TEST_APP_VERSION,
    issue_device_key: bool = True,
) -> services.SessionStart:
    """Sign ``customer`` in through ``services.start_customer_session`` and remember the raw secrets, so
    :func:`raw_refresh` and :func:`raw_device_key` can read them later."""
    started = services.start_customer_session(
        customer.pk, fid=fid, platform=platform, app_version=app_version, issue_device_key=issue_device_key
    )
    _refresh_tokens[started.session.pk] = started.raw_refresh
    if started.raw_device_key is not None and started.session.device_id is not None:
        _device_keys[started.session.device_id] = started.raw_device_key
    return started


def raw_device_key(device: Device) -> str:
    """The raw device key :func:`start_test_session` issued for ``device``."""
    return _device_keys[device.pk]


def device_headers(
    fid: str = TEST_FID,
    platform: str = TEST_PLATFORM,
    version: str = TEST_APP_VERSION,
    key: str | None = None,
) -> dict[str, Any]:
    """``APIClient`` keyword arguments for the customer device headers (customer contract §1.2)."""
    headers: dict[str, Any] = {
        "HTTP_X_GD_DEVICE_ID": fid,
        "HTTP_X_GD_PLATFORM": platform,
        "HTTP_X_GD_APP_VERSION": version,
    }
    if key is not None:
        headers["HTTP_X_GD_DEVICE_KEY"] = key
    return headers
