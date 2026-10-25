"""factory-boy factories for the integrations models (``<Factory>.create(...)``)."""

from typing import Any

from django.contrib.auth import get_user_model
from factory.declarations import LazyFunction, Sequence
from factory.django import DjangoModelFactory

from glowderma.core import enums
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox


def _staff(**extra: Any) -> Any:
    """A staff actor without importing ``accounts`` (integrations sits below it in the layer contract)."""
    return get_user_model().objects.create_user(f"staff-{get_user_model().objects.count() + 1}@x.uz", **extra)


class WebhookInboxFactory(DjangoModelFactory[WebhookInbox]):
    class Meta:
        model = WebhookInbox

    provider = WebhookInbox.Provider.CLICK
    event_key = Sequence(lambda n: f"complete:{n + 1}:10248")  # type: ignore[no-untyped-call]
    method = "POST"
    path = "/api/v1/webhooks/click/"
    headers = LazyFunction(lambda: {"Content-Type": "application/x-www-form-urlencoded"})  # type: ignore[no-untyped-call]
    payload = LazyFunction(lambda: {"click_trans_id": 1})  # type: ignore[no-untyped-call]
    raw_body = "click_trans_id=1"
    entity_ref = "order:10248"
    result = LazyFunction(  # type: ignore[no-untyped-call]
        lambda: {"status": 200, "body": {"error": 0}, "outcome": "accepted", "error_code": None}
    )


class JobFactory(DjangoModelFactory[Job]):
    class Meta:
        model = Job

    kind = enums.JobKind.BILLZ_IMPORT
    state = enums.JobState.QUEUED
    params = LazyFunction(lambda: {"mode": "delta"})  # type: ignore[no-untyped-call]


class IntegrationStateFactory(DjangoModelFactory[IntegrationState]):
    class Meta:
        model = IntegrationState
        django_get_or_create = ("provider",)

    provider = IntegrationState.Provider.BILLZ


def staff_actor(**extra: Any) -> Any:
    return _staff(**extra)
