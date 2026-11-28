"""``loyalty.storefront``: a new cashback policy version changes the landing's tiers."""

from __future__ import annotations

from typing import Any

import pytest

from glowderma.core.tests.storefront_support import revalidated
from glowderma.loyalty.tests.factories import CashbackPolicyFactory

pytestmark = pytest.mark.django_db


def test_a_new_policy_version(django_capture_on_commit_callbacks: Any) -> None:
    assert "shop:home" in revalidated(
        django_capture_on_commit_callbacks, lambda: CashbackPolicyFactory.create()
    )
