import hashlib
from collections.abc import Iterator

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.models import AuthSession, Customer, StaffUser
from glowderma.accounts.tests.factories import (
    AuthSessionFactory,
    CustomerFactory,
    StaffUserFactory,
    device_headers,
    raw_device_key,
    raw_refresh,
    start_test_session,
)
from glowderma.core import qr_store
from glowderma.loyalty.models import PosDevice

POS_TEST_KEY = "gdpos_test0001_pytest-pos-key-00000000000000000"  # noqa: S105 - test-only


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    cache.clear()
    qr_store.clear_local()
    yield
    cache.clear()
    qr_store.clear_local()


@pytest.fixture(scope="session", autouse=True)
def static_directory() -> None:
    settings.STATIC_ROOT.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def media_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Image factories and the demo seeders write real files: keep them out of the repository's MEDIA_ROOT."""
    with override_settings(MEDIA_ROOT=tmp_path_factory.mktemp("media")):
        yield


def _json_client() -> APIClient:
    client = APIClient()
    client.default_format = "json"
    return client


@pytest.fixture
def staff(db: None) -> StaffUser:
    """An active staff user whose password is ``factories.TEST_PASSWORD``."""
    return StaffUserFactory.create()


@pytest.fixture
def staff_session(staff: StaffUser) -> tuple[AuthSession, str]:
    """An open session of ``staff`` together with its raw refresh token."""
    session = AuthSessionFactory.create(staff=staff)
    return session, raw_refresh(session)


@pytest.fixture
def staff_token(staff_session: tuple[AuthSession, str]) -> str:
    """A valid access JWT string for ``staff_session``."""
    access, _ = tokens.issue_access(staff_session[0])
    return access


@pytest.fixture
def staff_client(staff_token: str) -> APIClient:
    """A JSON ``APIClient`` sending ``Authorization: Bearer`` and ``X-GD-Internal-Secret`` for ``staff``.

    Named ``staff_client`` so that pytest-django's ``admin_client`` (a Django ``Client`` logged into the
    Django admin) stays available for ``/django-admin/`` pages.
    """
    client = _json_client()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {staff_token}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


@pytest.fixture
def anon_client() -> APIClient:
    """A JSON ``APIClient`` without credentials."""
    return _json_client()


# --- M2 customer audience (plan §1 P0)
# -----------------------------------------------------------------------


@pytest.fixture
def customer(db: None) -> Customer:
    """An active customer."""
    return CustomerFactory.create()


@pytest.fixture
def customer_session(customer: Customer) -> tuple[AuthSession, str]:
    """An open customer session on device ``fid-test-0001`` (ios, 1.0.0) with a device key, and its raw
    refresh token."""
    started = start_test_session(customer)
    return started.session, started.raw_refresh


@pytest.fixture
def customer_token(customer_session: tuple[AuthSession, str]) -> str:
    """A valid customer access JWT for ``customer_session``."""
    access, _ = tokens.issue_access(customer_session[0])
    return access


@pytest.fixture
def customer_client(customer_session: tuple[AuthSession, str], customer_token: str) -> APIClient:
    """A JSON ``APIClient`` with the customer bearer, the session's device headers and its known device key."""
    session = customer_session[0]
    if session.device is None:  # pragma: no cover - start_customer_session always binds a device
        raise RuntimeError("customer session without a device")
    client = _json_client()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {customer_token}",
        **device_headers(key=raw_device_key(session.device)),
    )
    return client


@pytest.fixture
def public_client() -> APIClient:
    """An anonymous app client: device headers of another install, no bearer."""
    client = _json_client()
    client.credentials(**device_headers(fid="fid-public-0001", platform="android"))
    return client


@pytest.fixture
def pos_key() -> str:
    """The raw API key of ``pos_device`` (format ``gdpos_<8-char prefix>_<32 base64url chars>``)."""
    return POS_TEST_KEY


@pytest.fixture
def pos_device(db: None, pos_key: str) -> PosDevice:
    """An active POS device whose key hash matches ``pos_key``."""
    return PosDevice.objects.create(
        name="Kassa test",
        key_prefix=pos_key.split("_")[1],
        key_hash=hashlib.sha256(pos_key.encode()).hexdigest(),
    )


@pytest.fixture
def pos_client(pos_device: PosDevice, pos_key: str) -> APIClient:
    """A JSON ``APIClient`` sending ``Authorization: Api-Key <key>`` for ``pos_device``."""
    client = _json_client()
    client.credentials(HTTP_AUTHORIZATION=f"Api-Key {pos_key}")
    return client
