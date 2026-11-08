"""The campaign approval workflow (admin contract §2.8): ownership of drafts, the superuser-only routes,
the mandatory test, revision binding, approval without dispatch, the beat's clock, return to draft and the
two races (double approve, double beat claim)."""

# ruff: noqa: F811 - the production settings fixtures are imported from the core settings tests by name

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from io import BytesIO
from types import ModuleType
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts.models import Device, StaffUser
from glowderma.accounts.tests.conftest import bearer_client
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory, StaffUserFactory
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tests.test_settings import (  # noqa: F401 - shared production settings fixtures
    load_fresh_production,
    production_env,
)
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.notifications import services, state
from glowderma.notifications.models import Campaign, CampaignTest, InboxNotification, TestDevice
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CampaignTestFactory,
    CustomerSegmentFactory,
    ScheduledCampaignFactory,
    SendingCampaignFactory,
    TestDeviceFactory,
    TestedCampaignFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/"
KEY = "6f1c2a0e-9b8d-4c37-8a1e-0123456789ab"
DELAY = timedelta(minutes=5)


def image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (40, 30), "#d6b2c4").save(output, format="PNG")
    return output.getvalue()


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def form(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "general",
        "title": "Yangi mahsulotlar keldi",
        "body": "Serumlar va tonerlar bo‘limida yangi mahsulotlar. Ilovada ko‘ring.",
        "audience.type": "all",
    }
    data.update(overrides)
    return data


def patch(client: APIClient, campaign_id: int, data: dict[str, Any]) -> Any:
    return client.patch(f"{URL}{campaign_id}/", data, format="multipart")


def action(client: APIClient, campaign_id: int, name: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(f"{URL}{campaign_id}/{name}/", body or {}, format="json")


def approve(client: APIClient, campaign: Campaign, revision: int | None = None) -> Any:
    return action(
        client, campaign.pk, "approve", {"expected_revision": revision or campaign.content_revision}
    )


def pushable_test_device(**device: Any) -> TestDevice:
    return TestDeviceFactory.create(device=DeviceFactory.create(**device))


def audits(action_name: str) -> int:
    return AuditEntry.objects.filter(action=action_name).count()


# --- ownership and the superuser gate ----------------------------------------------------------------------


def test_only_the_author_or_a_superuser_edits_or_deletes_a_draft(
    staff_client: APIClient, staff: StaffUser, superuser_client: APIClient, clock: Any
) -> None:
    mine = CampaignFactory.create(author=staff)
    theirs = CampaignFactory.create(author=StaffUserFactory.create())
    forbidden = patch(staff_client, theirs.pk, {"title": "Boshqa sarlavha", "expected_revision": "1"})
    assert forbidden.status_code == 403 and forbidden.data["code"] == "permission_denied"
    assert staff_client.delete(f"{URL}{theirs.pk}/").status_code == 403
    theirs.refresh_from_db()
    assert theirs.title != "Boshqa sarlavha" and not audits("notification.draft_updated")
    allowed = patch(staff_client, mine.pk, {"title": "Mening sarlavham", "expected_revision": "1"})
    assert allowed.status_code == 200, allowed.data
    assert allowed.data["title"] == "Mening sarlavham" and allowed.data["revision"] == 2
    # a superuser edits and deletes anyone's draft
    assert (
        patch(
            superuser_client, theirs.pk, {"body": "Egasi yozgan matn.", "expected_revision": "1"}
        ).status_code
        == 200
    )
    assert superuser_client.delete(f"{URL}{theirs.pk}/").status_code == 204
    assert not Campaign.objects.filter(pk=theirs.pk).exists()
    assert audits("notification.draft_deleted") == 1
    # every staff member still sees every campaign
    assert staff_client.get(f"{URL}{mine.pk}/").status_code == 200
    assert [row["id"] for row in staff_client.get(URL).data["results"]] == [mine.pk]


SUPERUSER_ROUTES = [
    ("post", "{id}/test/", {"device_ids": [1]}),
    ("post", "{id}/tests/1/confirm/", None),
    ("post", "{id}/approve/", {"expected_revision": 1}),
    ("post", "{id}/return-to-draft/", None),
    ("post", "{id}/cancel/", None),
    ("post", "{id}/stop/", None),
    ("get", "test-devices/", None),
    ("post", "test-devices/", {"phone": "+998901234567", "label": "Telefon"}),
    ("delete", "test-devices/1/", None),
]


@pytest.mark.parametrize(("method", "route", "body"), SUPERUSER_ROUTES)
def test_every_superuser_route_answers_403_to_a_plain_staff_member(
    staff_client: APIClient, method: str, route: str, body: dict[str, Any] | None
) -> None:
    """A manager is not a superuser either: ``is_superuser`` alone opens these routes."""
    campaign = TestedCampaignFactory.create()
    manager_client = bearer_client(StaffUserFactory.create(is_manager=True))
    for client in (staff_client, manager_client):
        response = getattr(client, method)(URL + route.format(id=campaign.pk), body, format="json")
        assert response.status_code == 403, (route, response.data)
        assert (response.data["code"], response.data["message"]) == ("permission_denied", "Ruxsat yo‘q.")
    campaign.refresh_from_db()
    assert campaign.state == "tested" and not AuditEntry.objects.exclude(action="").exists()


# --- the mandatory test ------------------------------------------------------------------------------------


def test_a_draft_cannot_be_approved_before_a_confirmed_test(
    superuser_client: APIClient, clock: Any, fcm: FakeFcm
) -> None:
    campaign = CampaignFactory.create()
    refused = approve(superuser_client, campaign)
    assert refused.status_code == 409, refused.data
    assert (refused.data["code"], refused.data["message"]) == (
        "invalid_transition",
        services.MESSAGE_NOT_TESTED,
    )
    campaign.refresh_from_db()
    assert campaign.state == "draft" and campaign.approved_at is None
    assert OutboxMessage.objects.count() == 0 and fcm.sent == [] and not audits("notification.approved")


def test_send_test_pushes_to_the_registered_devices_and_records_the_outcome(
    superuser_client: APIClient, superuser: StaffUser, clock: Any, fcm: FakeFcm
) -> None:
    good = pushable_test_device(fcm_token="tok-good")  # noqa: S106
    dead = pushable_test_device(fcm_token="tok-bad")  # noqa: S106
    silent = pushable_test_device()
    silent.device.fcm_token, silent.device.push_enabled = "", False
    silent.device.save()
    campaign = CampaignFactory.create(kind="promo", link_kind="url", link_url="https://glowderma.uz/aksiya")
    response = action(superuser_client, campaign.pk, "test", {"device_ids": [good.pk, dead.pk, silent.pk]})
    assert response.status_code == 200, response.data
    assert response.data["state"] == "draft"  # a test does not change the state
    tests = {t["test_device_id"]: t for t in response.data["tests"]}
    assert set(tests) == {good.pk, dead.pk, silent.pk}
    assert tests[good.pk]["fcm_ok"] is True and tests[good.pk]["fcm_error"] == ""
    assert tests[good.pk]["sent_by"] == {"id": superuser.pk, "display_name": "Egasi"}
    assert tests[good.pk]["device_label"] == good.label
    assert "phone" not in tests[good.pk]  # the tester's number stays in the superuser-only registry
    assert (tests[dead.pk]["fcm_ok"], tests[dead.pk]["fcm_error"]) == (False, "unregistered")
    assert (tests[silent.pk]["fcm_ok"], tests[silent.pk]["fcm_error"]) == (False, "no_device")
    assert all(t["revision"] == 1 and t["confirmed_at"] is None for t in tests.values())
    # the tester got a real inbox row the deep link opens; the dead token pruned its device
    [message] = fcm.sent
    inbox = InboxNotification.objects.get(pk=int(message.data["inbox_id"]))
    assert inbox.customer_id == good.device.customer_id and inbox.push_state == "sent"
    assert inbox.dedupe_key == f"campaign-test:{campaign.pk}:{tests[good.pk]['id']}"
    assert (inbox.kind, inbox.link, inbox.source_id) == (
        "promo",
        {"kind": "url", "url": campaign.link_url},
        campaign.pk,
    )
    assert message.data["campaign_id"] == str(campaign.pk) and message.data["link_url"] == campaign.link_url
    assert message.android_channel == "promo"
    dead_device = Device.objects.get(pk=dead.device_id)
    assert dead_device.fcm_token == "" and not dead_device.push_enabled
    assert CampaignTest.objects.get(pk=tests[dead.pk]["id"]).inbox.push_state == "failed"  # type: ignore[union-attr]
    entry = AuditEntry.objects.get(action="notification.test_sent")
    assert entry.actor_id == superuser.pk and len(entry.after["tests"]) == 3  # type: ignore[index]
    # no recipients, nothing enqueued: a test is not a send
    assert OutboxMessage.objects.count() == 0 and campaign.recipients.count() == 0


def test_send_test_with_fcm_down_records_undelivered_tests_and_answers_503(
    superuser_client: APIClient, clock: Any, fcm: FakeFcm
) -> None:
    down = pushable_test_device(fcm_token="tok-down")  # noqa: S106
    campaign = CampaignFactory.create()
    response = action(superuser_client, campaign.pk, "test", {"device_ids": [down.pk]})
    assert response.status_code == 503 and response.data["code"] == "provider_unavailable"
    assert response.data["message"] == services.MESSAGE_FCM_DOWN
    test = CampaignTest.objects.get(campaign=campaign)
    assert (test.fcm_ok, test.fcm_error) == (False, "unavailable")
    assert test.inbox is not None and test.inbox.push_state == "failed"
    confirm = action(superuser_client, campaign.pk, f"tests/{test.pk}/confirm")
    assert confirm.status_code == 409 and confirm.data["message"] == services.MESSAGE_TEST_UNDELIVERED


def test_send_test_validation(superuser_client: APIClient, clock: Any, fcm: FakeFcm) -> None:
    campaign = CampaignFactory.create()
    removed = pushable_test_device()
    services.remove_test_device(removed.pk, actor=None)
    assert errors(action(superuser_client, campaign.pk, "test", {})) == [
        ("device_ids", "required", "Majburiy maydon.")
    ]
    assert errors(action(superuser_client, campaign.pk, "test", {"device_ids": []}))[0][1] == "empty"
    assert errors(action(superuser_client, campaign.pk, "test", {"device_ids": list(range(1, 12))})) == [
        ("device_ids", "max_items", "Ko‘pi bilan 10 ta.")
    ]
    unknown = action(superuser_client, campaign.pk, "test", {"device_ids": [removed.pk]})
    assert errors(unknown) == [("device_ids", "does_not_exist", "Test qurilmasi topilmadi.")]
    silent = pushable_test_device(push_enabled=False, fcm_token="")
    nothing = action(superuser_client, campaign.pk, "test", {"device_ids": [silent.pk]})
    assert nothing.status_code == 409 and nothing.data["message"] == services.MESSAGE_NO_TEST_DEVICES
    scheduled = ScheduledCampaignFactory.create()
    wrong_state = action(superuser_client, scheduled.pk, "test", {"device_ids": [silent.pk]})
    assert wrong_state.status_code == 409 and wrong_state.data["code"] == "invalid_transition"
    assert action(superuser_client, 999_999, "test", {"device_ids": [silent.pk]}).status_code == 404


def test_confirm_needs_a_delivered_test_of_the_current_revision(
    superuser_client: APIClient, superuser: StaffUser, clock: Any
) -> None:
    campaign = CampaignFactory.create(content_revision=2)
    stale = CampaignTestFactory.create(campaign=campaign, revision=1)
    undelivered = CampaignTestFactory.create(campaign=campaign, fcm_ok=False, fcm_error="unregistered")
    current = CampaignTestFactory.create(campaign=campaign)
    other = CampaignTestFactory.create()
    response = action(superuser_client, campaign.pk, f"tests/{stale.pk}/confirm")
    assert response.status_code == 409 and response.data["message"] == services.MESSAGE_TEST_STALE
    response = action(superuser_client, campaign.pk, f"tests/{undelivered.pk}/confirm")
    assert response.status_code == 409 and response.data["message"] == services.MESSAGE_TEST_UNDELIVERED
    assert action(superuser_client, campaign.pk, f"tests/{other.pk}/confirm").status_code == 404
    campaign.refresh_from_db()
    assert campaign.state == "draft" and not audits("notification.test_confirmed")
    response = action(superuser_client, campaign.pk, f"tests/{current.pk}/confirm")
    assert response.status_code == 200, response.data
    assert (response.data["state"], response.data["tested_revision"], response.data["revision"]) == (
        "tested",
        2,
        2,
    )
    assert response.data["test_confirmed_at"] == ANCHOR.isoformat()
    assert response.data["test_confirmed_by"] == {"id": superuser.pk, "display_name": "Egasi"}
    confirmed = next(t for t in response.data["tests"] if t["id"] == current.pk)
    assert confirmed["confirmed_at"] == ANCHOR.isoformat() and confirmed["confirmed_by"]["id"] == superuser.pk
    # idempotent
    again = action(superuser_client, campaign.pk, f"tests/{current.pk}/confirm")
    assert again.status_code == 200 and again.data == response.data
    assert audits("notification.test_confirmed") == 1


# --- revisions ---------------------------------------------------------------------------------------------


def test_editing_the_content_invalidates_the_test_confirmation(
    superuser_client: APIClient, clock: Any
) -> None:
    campaign = TestedCampaignFactory.create()
    CampaignTestFactory.create(campaign=campaign, confirmed_at=ANCHOR)
    # a schedule-only change keeps the confirmation
    response = patch(
        superuser_client, campaign.pk, {"scheduled_at": "2026-10-26T10:00:00+05:00", "expected_revision": "1"}
    )
    assert response.status_code == 200, response.data
    assert (response.data["state"], response.data["revision"], response.data["tested_revision"]) == (
        "tested",
        1,
        1,
    )
    assert response.data["scheduled_at"] == "2026-10-26T10:00:00+05:00"
    # a body change bumps the revision and drops the confirmation: the campaign is a draft again
    response = patch(
        superuser_client, campaign.pk, {"body": "Butunlay boshqa matn.", "expected_revision": "1"}
    )
    assert response.status_code == 200, response.data
    assert (response.data["state"], response.data["revision"], response.data["tested_revision"]) == (
        "draft",
        2,
        None,
    )
    assert response.data["test_confirmed_at"] is None and response.data["body"] == "Butunlay boshqa matn."
    assert response.data["scheduled_at"] == "2026-10-26T10:00:00+05:00"  # kept: not submitted
    assert len(response.data["tests"]) == 1  # the old test stays on record, for revision 1
    entry = AuditEntry.objects.filter(action="notification.draft_updated").order_by("-id").first()
    assert entry is not None and entry.after is not None
    assert (entry.after["body"], entry.after["revision"], entry.after["state"]) == (
        "Butunlay boshqa matn.",
        2,
        "draft",
    )
    assert entry.before is not None and entry.before["revision"] == 1
    refused = approve(superuser_client, campaign, revision=2)
    assert refused.status_code == 409 and refused.data["code"] == "invalid_transition"


def test_a_stale_revision_is_refused_on_patch_and_approve(superuser_client: APIClient, clock: Any) -> None:
    campaign = TestedCampaignFactory.create(content_revision=3, tested_revision=3)
    response = patch(superuser_client, campaign.pk, {"title": "Yangi sarlavha", "expected_revision": "2"})
    assert response.status_code == 409
    assert (response.data["code"], response.data["message"]) == ("conflict", services.MESSAGE_STALE_REVISION)
    response = approve(superuser_client, campaign, revision=2)
    assert response.status_code == 409 and response.data["message"] == services.MESSAGE_STALE_REVISION
    campaign.refresh_from_db()
    assert (campaign.state, campaign.title, campaign.content_revision) == ("tested", campaign.title, 3)
    assert errors(patch(superuser_client, campaign.pk, {"title": "Yangi"})) == [
        ("expected_revision", "required", "Majburiy maydon.")
    ]


def test_patch_keeps_a_requested_time_that_passed_unless_it_is_resubmitted(
    superuser_client: APIClient, clock: Any
) -> None:
    """``in_past`` applies to a submitted ``scheduled_at`` only: a draft whose requested time went by is still
    editable on another field (the dispatch uses ``max(now + delay, scheduled_at)`` anyway)."""
    campaign = CampaignFactory.create(scheduled_at=ANCHOR + timedelta(hours=1))
    clock(ANCHOR + timedelta(hours=2))
    response = patch(
        superuser_client, campaign.pk, {"expected_revision": "1", "title": "Tuzatilgan sarlavha"}
    )
    assert response.status_code == 200, response.data
    assert response.data["title"] == "Tuzatilgan sarlavha" and response.data["revision"] == 2
    assert response.data["scheduled_at"] == (ANCHOR + timedelta(hours=1)).isoformat()
    # resubmitting the stale time itself is still refused; clearing it is fine
    stale = patch(
        superuser_client,
        campaign.pk,
        {"expected_revision": "2", "scheduled_at": (ANCHOR + timedelta(hours=1)).isoformat()},
    )
    assert errors(stale) == [("scheduled_at", "in_past", "Kelajakdagi vaqtni tanlang.")]
    cleared = patch(superuser_client, campaign.pk, {"expected_revision": "2", "scheduled_at": ""})
    assert cleared.status_code == 200 and cleared.data["scheduled_at"] is None


def test_approve_outside_tested_names_the_reason(superuser_client: APIClient, clock: Any) -> None:
    draft = CampaignFactory.create()
    response = approve(superuser_client, draft)
    assert response.status_code == 409
    assert (response.data["code"], response.data["message"]) == (
        "invalid_transition",
        services.MESSAGE_NOT_TESTED,
    )
    for finished in (
        SendingCampaignFactory.create(),
        SendingCampaignFactory.create(state="sent", sent_at=ANCHOR, sent=1, failed=0),
    ):
        response = approve(superuser_client, finished)
        assert response.status_code == 409, response.data
        assert (response.data["code"], response.data["message"]) == (
            "invalid_transition",
            state.MESSAGE_NOT_APPROVABLE,
        )


def test_patch_merges_the_form_replaces_the_image_and_validates(
    staff_client: APIClient, staff: StaffUser, clock: Any
) -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar", min_online_orders=None)
    member = CustomerFactory.create()
    DeviceFactory.create(customer=member)
    campaign = CampaignFactory.create(author=staff)
    response = patch(
        staff_client,
        campaign.pk,
        {
            "expected_revision": "1",
            "audience.type": "segment",
            "audience.segment_id": str(segment.pk),
            "image": SimpleUploadedFile("promo.png", image_bytes(), content_type="image/png"),
        },
    )
    assert response.status_code == 200, response.data
    assert response.data["audience"]["segment_name"] == "Doimiy mijozlar"
    assert response.data["recipient_count"] == 0 and response.data["image_url"].endswith(".png")
    assert response.data["revision"] == 2
    campaign.refresh_from_db()
    stored = campaign.image.name or ""
    # an invalid merged form is a 400 and changes nothing
    bad = patch(staff_client, campaign.pk, {"expected_revision": "2", "audience.type": "segment"})
    assert errors(bad) == [("audience.segment_id", "required", "Majburiy maydon.")]
    assert patch(staff_client, campaign.pk, {"expected_revision": "2", "title": "ab"}).status_code == 400
    assert patch(staff_client, campaign.pk, {"expected_revision": "x", "title": "Yangi"}).status_code == 400
    # remove_image drops the file; an unchanged form bumps nothing
    response = patch(staff_client, campaign.pk, {"expected_revision": "2", "remove_image": "true"})
    assert (
        response.status_code == 200 and response.data["image_url"] is None and response.data["revision"] == 3
    )
    campaign.refresh_from_db()
    assert not campaign.image and not campaign.image.storage.exists(stored)
    response = patch(staff_client, campaign.pk, {"expected_revision": "3"})
    assert response.status_code == 200 and response.data["revision"] == 3
    # JSON is not accepted (multipart only), a scheduled campaign is not editable
    assert (
        staff_client.patch(f"{URL}{campaign.pk}/", {"expected_revision": 3}, format="json").status_code == 415
    )
    scheduled = ScheduledCampaignFactory.create(author=staff)
    locked = patch(staff_client, scheduled.pk, {"expected_revision": "1", "title": "Boshqa sarlavha"})
    assert locked.status_code == 409 and locked.data["message"] == state.MESSAGE_NOT_EDITABLE
    assert staff_client.delete(f"{URL}{scheduled.pk}/").status_code == 409
    assert patch(staff_client, 999_999, {"expected_revision": "1"}).status_code == 404
    assert staff_client.delete(f"{URL}999999/").status_code == 404


# --- approval without dispatch and the beat's clock -------------------------------------------------------


def test_approve_schedules_without_dispatching(
    superuser_client: APIClient,
    superuser: StaffUser,
    clock: Any,
    fcm: FakeFcm,
    django_capture_on_commit_callbacks: Any,
) -> None:
    for _ in range(2):
        DeviceFactory.create(customer=CustomerFactory.create())
    campaign = TestedCampaignFactory.create(recipient_count=0)
    with django_capture_on_commit_callbacks(execute=True):
        response = approve(superuser_client, campaign)
    assert response.status_code == 200, response.data
    assert response.data["state"] == "scheduled"
    assert response.data["approved_at"] == ANCHOR.isoformat() and response.data["approved_revision"] == 1
    assert response.data["approved_by"] == {"id": superuser.pk, "display_name": "Egasi"}
    assert response.data["dispatch_after"] == (ANCHOR + DELAY).isoformat()
    assert response.data["recipient_count"] == 2  # re-estimated at approval
    assert (response.data["started_at"], response.data["sent"], response.data["attempted"]) == (
        None,
        None,
        None,
    )
    # nothing left the building
    assert OutboxMessage.objects.count() == 0 and fcm.sent == [] and campaign.recipients.count() == 0
    assert not InboxNotification.objects.exists()
    entry = AuditEntry.objects.get(action="notification.approved")
    assert entry.actor_id == superuser.pk and entry.after is not None
    assert (
        entry.after["dispatch_after"] == (ANCHOR + DELAY).isoformat()
        and entry.after["approved_revision"] == 1
    )
    # a repeat on the approved revision answers unchanged, without a second audit row
    again = approve(superuser_client, campaign)
    assert again.status_code == 200 and again.data == response.data
    assert audits("notification.approved") == 1
    # the beat does not start it before dispatch_after, and does at that instant
    clock(ANCHOR + DELAY - timedelta(seconds=1))
    assert services.start_due_campaigns() == []
    campaign.refresh_from_db()
    assert campaign.state == "scheduled" and OutboxMessage.objects.count() == 0
    clock(ANCHOR + DELAY)
    assert services.start_due_campaigns() == [campaign.pk]
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed, campaign.recipient_count) == ("sending", 0, 0, 2)
    assert campaign.started_at == ANCHOR + DELAY
    message = OutboxMessage.objects.get(topic="campaign.send_batch")
    assert message.payload == {"campaign_id": campaign.pk, "batch_no": 0}
    started = AuditEntry.objects.get(action="notification.send_started")
    assert (
        started.actor_type == "system" and started.after is not None and started.after["state"] == "sending"
    )


def test_dispatch_waits_for_the_requested_time_when_it_is_later(
    superuser_client: APIClient, clock: Any
) -> None:
    requested = datetime(2026, 10, 26, 10, tzinfo=TASHKENT)
    campaign = TestedCampaignFactory.create(scheduled_at=requested)
    response = approve(superuser_client, campaign)
    assert response.status_code == 200 and response.data["dispatch_after"] == requested.isoformat()
    soon = TestedCampaignFactory.create(scheduled_at=ANCHOR + timedelta(minutes=1))
    response = approve(superuser_client, soon)
    assert response.data["dispatch_after"] == (ANCHOR + DELAY).isoformat()  # never inside the window


@override_settings(CAMPAIGN_APPROVAL_DELAY_MINUTES=0)
def test_a_zero_delay_dispatches_at_the_next_beat(superuser_client: APIClient, clock: Any) -> None:
    campaign = TestedCampaignFactory.create()
    assert approve(superuser_client, campaign).data["dispatch_after"] == ANCHOR.isoformat()
    assert superuser_client.get(URL + "meta/").data["approval_delay_minutes"] == 0
    assert services.start_due_campaigns() == [campaign.pk]


def test_approve_refuses_a_deleted_segment_and_a_missing_body(
    superuser_client: APIClient, clock: Any
) -> None:
    campaign = TestedCampaignFactory.create(
        audience_type="segment", audience_segment=None, audience_segment_name="O‘chirilgan"
    )
    response = approve(superuser_client, campaign)
    assert response.status_code == 409 and response.data["message"] == services.MESSAGE_SEGMENT_GONE
    assert errors(action(superuser_client, campaign.pk, "approve", {})) == [
        ("expected_revision", "required", "Majburiy maydon.")
    ]
    assert approve(superuser_client, CampaignFactory.build(pk=999_999)).status_code == 404


def test_production_settings_refuse_a_short_approval_delay(
    production_env: None, monkeypatch: pytest.MonkeyPatch, load_fresh_production: Callable[[], ModuleType]
) -> None:
    monkeypatch.setenv("CAMPAIGN_APPROVAL_DELAY_MINUTES", "4")
    with pytest.raises(ImproperlyConfigured, match="CAMPAIGN_APPROVAL_DELAY_MINUTES must be at least 5"):
        load_fresh_production()
    monkeypatch.setenv("CAMPAIGN_APPROVAL_DELAY_MINUTES", "5")
    assert load_fresh_production().CAMPAIGN_APPROVAL_DELAY_MINUTES == 5
    monkeypatch.setenv("CAMPAIGN_APPROVAL_DELAY_MINUTES", "1441")
    with pytest.raises(ImproperlyConfigured, match="must be between 0 and 1440"):
        load_fresh_production()


# --- return to draft ----------------------------------------------------------------------------------------


def test_return_to_draft_clears_the_approval_and_bumps_the_revision(
    superuser_client: APIClient, superuser: StaffUser, clock: Any
) -> None:
    campaign = ScheduledCampaignFactory.create(scheduled_at=ANCHOR + timedelta(days=1))
    response = action(superuser_client, campaign.pk, "return-to-draft")
    assert response.status_code == 200, response.data
    assert (response.data["state"], response.data["revision"], response.data["tested_revision"]) == (
        "draft",
        2,
        None,
    )
    assert response.data["approved_at"] is None and response.data["dispatch_after"] is None
    assert response.data["scheduled_at"] == (ANCHOR + timedelta(days=1)).isoformat()  # the request stays
    entry = AuditEntry.objects.get(action="notification.returned_to_draft")
    assert entry.actor_id == superuser.pk and entry.before == {"state": "scheduled", "revision": 1}
    again = action(superuser_client, campaign.pk, "return-to-draft")  # a draft answers unchanged
    assert (
        again.status_code == 200
        and again.data == response.data
        and audits("notification.returned_to_draft") == 1
    )
    # a cancelled campaign that never started comes back too; a tested one is refused
    cancelled = ScheduledCampaignFactory.create()
    services.cancel_campaign(cancelled.pk, actor=superuser)
    back = action(superuser_client, cancelled.pk, "return-to-draft")
    assert back.status_code == 200 and back.data["state"] == "draft" and back.data["cancelled_at"] is None
    tested = TestedCampaignFactory.create()
    refused = action(superuser_client, tested.pk, "return-to-draft")
    assert refused.status_code == 409 and refused.data["code"] == "invalid_transition"
    stopped = SendingCampaignFactory.create(
        state="cancelled", stop_requested_at=ANCHOR, stopped_at=ANCHOR, cancelled_at=ANCHOR
    )
    assert action(superuser_client, stopped.pk, "return-to-draft").status_code == 409
    assert action(superuser_client, 999_999, "return-to-draft").status_code == 404


# --- races --------------------------------------------------------------------------------------------------


def _run_in_threads(targets: list[Callable[[], Any]]) -> list[Any]:
    results: list[Any] = [None] * len(targets)
    gate = threading.Event()

    def runner(index: int, target: Callable[[], Any]) -> None:
        gate.wait(10)
        try:
            results[index] = target()
        except Exception as exc:  # noqa: BLE001 - surfaced through the results
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=runner, args=(index, target)) for index, target in enumerate(targets)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    return results


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_approvals_write_one_audit_row(clock: Any) -> None:
    owner = StaffUserFactory.create(is_superuser=True)
    campaign = TestedCampaignFactory.create()

    def approve_once() -> Campaign:
        return services.approve_campaign(campaign.pk, expected_revision=1, actor=owner)

    results = _run_in_threads([approve_once, approve_once])
    assert all(isinstance(result, Campaign) for result in results), results
    assert AuditEntry.objects.filter(action="notification.approved").count() == 1
    campaign.refresh_from_db()
    assert campaign.state == "scheduled" and campaign.approved_revision == 1


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_beats_claim_a_due_campaign_once(clock: Any) -> None:
    DeviceFactory.create(customer=CustomerFactory.create())
    campaign = ScheduledCampaignFactory.create(dispatch_after=ANCHOR - timedelta(minutes=1))
    results = _run_in_threads([services.start_due_campaigns, services.start_due_campaigns])
    assert sorted(len(result) for result in results) == [0, 1], results
    assert AuditEntry.objects.filter(action="notification.send_started").count() == 1
    assert OutboxMessage.objects.filter(topic="campaign.send_batch").count() == 1  # batch 0 enqueued once
    campaign.refresh_from_db()
    # the eager on-commit dispatch may already have run batch 0 (one recipient → sent)
    assert campaign.state in ("sending", "sent") and campaign.recipients.count() == 1
    assert campaign.batches_dispatched <= 1
