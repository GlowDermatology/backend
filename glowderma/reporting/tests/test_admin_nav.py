"""The ops console's sidebar badges (glowderma.reporting.admin_nav): cheap cached counts, hidden at zero."""

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.models import OutboxMessage
from glowderma.integrations.tests.factories import WebhookInboxFactory
from glowderma.payments.tests.factories import RefundFactory
from glowderma.reporting import admin_nav

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


def staff_request(**flags: Any) -> Any:
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(**{"is_staff": True, **flags})
    return request


def test_badges_are_hidden_at_zero() -> None:
    request = staff_request()
    assert admin_nav.dead_outbox(request) is None
    assert admin_nav.failed_webhooks(request) is None
    assert admin_nav.refunds_needing_action(request) is None


def test_badges_count_and_cache_for_a_minute(django_assert_num_queries: Any) -> None:
    OutboxMessage.objects.create(topic="billz.push_sale", payload={}, dedupe_key="a", status="dead")
    OutboxMessage.objects.create(topic="billz.push_sale", payload={}, dedupe_key="b", status="done")
    WebhookInboxFactory.create(result={"status": 200, "body": {}, "outcome": "rejected", "error_code": "-5"})
    WebhookInboxFactory.create(result={"status": 200, "body": {}, "outcome": "accepted", "error_code": None})
    RefundFactory.create(failed=True)
    request = staff_request()
    assert admin_nav.dead_outbox(request) == "1"
    assert admin_nav.failed_webhooks(request) == "1"
    assert admin_nav.refunds_needing_action(request) == "1"
    OutboxMessage.objects.create(topic="billz.push_sale", payload={}, dedupe_key="c", status="dead")
    with django_assert_num_queries(0):
        assert admin_nav.dead_outbox(request) == "1"  # cached
    cache.clear()
    assert admin_nav.dead_outbox(request) == "2"


def test_badges_need_an_active_staff_user() -> None:
    OutboxMessage.objects.create(topic="t", payload={}, dedupe_key="a", status="dead")
    request = RequestFactory().get("/django-admin/")
    request.user = AnonymousUser()
    assert admin_nav.dead_outbox(request) is None
    assert admin_nav.dead_outbox(staff_request(is_staff=False)) is None


def test_cache_outage_falls_back_to_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    OutboxMessage.objects.create(topic="t", payload={}, dedupe_key="a", status="dead")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "get", broken)
    assert admin_nav.dead_outbox(staff_request()) == "1"
