"""Fixtures of the reporting tests.

``console`` is the one way the ops-console page tests sign in: a plain active ``is_staff`` user (never a
superuser, AGENTS.md) with a 2FA-verified console session — the root ``ops_client`` fixture
(``glowderma.accounts.tests.ops_helpers.ops_login``).
"""

import pytest
from django.test import Client


@pytest.fixture
def console(ops_client: Client) -> Client:
    return ops_client
