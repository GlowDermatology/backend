"""Fixtures of the reporting tests.

``console`` is the one way the ops-console page tests sign in: a plain active ``is_staff`` user (never a
superuser, AGENTS.md). When the console requires a second factor (package B: ``ops_client``), only this
fixture changes.
"""

from collections.abc import Iterator

import pytest
from django.test import Client

from glowderma.accounts.tests.factories import StaffUserFactory


@pytest.fixture
def console(db: None) -> Iterator[Client]:
    client = Client()
    client.force_login(StaffUserFactory.create(is_staff=True))
    yield client
