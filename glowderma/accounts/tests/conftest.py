"""Fixtures of the accounts tests: a manager (``StaffUser.is_manager``, admin contract §2.16), its client."""

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import AuthSessionFactory, StaffUserFactory


@pytest.fixture
def manager(db: None) -> StaffUser:
    """An active staff member who may manage staff (password ``factories.TEST_PASSWORD``)."""
    return StaffUserFactory.create(email="manager@glowderma.uz", full_name="Menejer", is_manager=True)


def bearer_client(user: StaffUser) -> APIClient:
    """A JSON ``APIClient`` signed in as ``user`` (a fresh password session)."""
    access, _ = tokens.issue_access(AuthSessionFactory.create(staff=user))
    client = APIClient()
    client.default_format = "json"
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


@pytest.fixture
def manager_client(manager: StaffUser) -> APIClient:
    return bearer_client(manager)
