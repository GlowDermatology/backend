"""The webhook list after the UX review: Uzbek outcomes with the provider code kept, an «HTTP javob» column
and the outcome as a sidebar filter that the dashboards' drill-down selects."""

import pytest
from django.test import Client
from django.urls import reverse

from glowderma.core.tests.test_console_i18n import visible_text
from glowderma.integrations.admin import WebhookInboxAdmin
from glowderma.integrations.models import WebhookInbox
from glowderma.integrations.tests.factories import WebhookInboxFactory

pytestmark = pytest.mark.django_db


def _row(outcome: str, code: str | None = None) -> WebhookInbox:
    result = {"status": 200, "body": {}, "outcome": outcome, "error_code": code}
    return WebhookInboxFactory.create(result=result)


def test_outcomes_read_in_uzbek_with_the_provider_code(ops_client: Client) -> None:
    _row("accepted")
    _row("rejected", "unknown_parcel")
    _row("error", "-32400")
    _row("duplicate")
    page = ops_client.get(reverse("admin:integrations_webhookinbox_changelist"))
    text = visible_text(page.content)
    for label in (
        "Qabul qilindi",
        "Rad etildi · unknown_parcel",
        "Ishlov xatosi · -32400",
        "Takroriy",
        "HTTP javob",
    ):
        assert label in text, label
    for english in ("Result status", "result status", "\naccepted\n", "rejected ·"):
        assert english not in f"\n{text}\n", english


def test_the_failed_drill_down_is_the_outcome_filter(ops_client: Client) -> None:
    _row("accepted")
    _row("rejected", "-1")
    _row("error", "-32400")
    url = reverse("admin:integrations_webhookinbox_changelist")
    page = ops_client.get(url, {"result__outcome__in": "rejected,error"})
    assert page.context["cl"].result_count == 2
    text = " ".join(visible_text(page.content).split())
    assert "Qo‘llangan filtrlar: Natija : Xato (rad etilgan yoki ishlov xatosi)" in text  # the applied bar
    assert ops_client.get(url, {"result__outcome__in": "accepted"}).context["cl"].result_count == 1
    assert any(
        entry.__name__ == "WebhookOutcomeFilter"
        for entry in WebhookInboxAdmin.list_filter
        if isinstance(entry, type)
    )
