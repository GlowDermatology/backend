from collections.abc import Iterator

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.tests.factories import AuthSessionFactory, StaffUserFactory, raw_refresh


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


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
