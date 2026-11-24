"""Campaign test pushes and their confirmation (admin contract §2.8)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import Conflict, ProviderUnavailable, field_error
from glowderma.integrations.fcm.ports import (
    FcmUnavailable,
)
from glowderma.integrations.fcm.registry import get_fcm_port
from glowderma.notifications import state, topics
from glowderma.notifications.models import (
    Campaign,
    CampaignTest,
    InboxNotification,
    TestDevice,
)
from glowderma.notifications.services import push_messages
from glowderma.notifications.services.campaigns import common, test_devices


def _test_token(test_device: TestDevice) -> str:
    """A test push goes only where the registry says ``is_pushable`` (:func:`selectors.test_devices`): the
    device still takes pushes **and** its customer is active (``device__customer`` is loaded)."""
    device = test_device.device
    if device.customer.status != enums.CustomerStatus.ACTIVE:
        return ""
    return push_messages.usable_token(device)


def _test_image(test: CampaignTest) -> dict[str, Any]:
    return {
        "test_id": test.pk,
        "test_device_id": test.test_device_id,
        "device_id": test.device_id,
        "revision": test.revision,
        "fcm_ok": test.fcm_ok,
        "fcm_error": test.fcm_error or None,
    }


def send_test(
    campaign_id: int, test_device_ids: Sequence[int], *, actor: Any, request: Any = None
) -> Campaign:
    """``POST notifications/{id}/test/`` (superuser): push the current revision of a ``draft`` / ``tested``
    campaign to the chosen active test devices.

    Transaction 1 locks the campaign, checks the state and writes one ``CampaignTest`` (``fcm_ok`` null) per
    device with a real ``InboxNotification`` for the tester (dedupe ``campaign-test:{campaign}:{test}``), so
    the deep link opens in the app; a device that lost its token, or whose customer is no longer active, gets
    ``fcm_ok = False`` ``no_device`` at once (the registry shows it ``is_pushable = false``). The FCM call
    runs **outside** the transaction through :func:`campaign_messages`; transaction 2
    records ``fcm_ok`` / ``fcm_error`` / ``provider_message_id`` and the inbox outcome, and prunes a dead
    token (``unregistered``). ``FcmUnavailable`` → every test ``fcm_ok = False`` ``unavailable`` and 503
    ``provider_unavailable``. Audit ``notification.test_sent``.
    """
    wanted = sorted({int(pk) for pk in test_device_ids})
    with transaction.atomic():
        campaign = common.lock_campaign(campaign_id)
        state.check("test", campaign.state)
        devices = {
            row.pk: row
            for row in TestDevice.objects.select_related("device", "device__customer").filter(
                pk__in=wanted, is_active=True
            )
        }
        if len(devices) != len(wanted):
            raise ValidationError(
                {
                    "device_ids": [
                        field_error("does_not_exist", test_devices.TEST_DEVICE_MESSAGES["does_not_exist"])
                    ]
                }
            )
        now = timeutils.now()
        tests: list[CampaignTest] = []
        targets: list[tuple[CampaignTest, InboxNotification, str]] = []
        for pk in wanted:
            test_device = devices[pk]
            token = _test_token(test_device)
            test = CampaignTest.objects.create(
                campaign=campaign,
                revision=campaign.content_revision,
                test_device=test_device,
                device=test_device.device,
                device_label=test_device.label,
                phone=test_device.phone,
                sent_by=actor,
                sent_at=now,
                fcm_ok=None if token else False,
                fcm_error="" if token else push_messages.NO_DEVICE,
            )
            tests.append(test)
            if not token:
                continue
            inbox = InboxNotification.objects.create(
                customer_id=test_device.device.customer_id,
                dedupe_key=topics.campaign_test_inbox_key(campaign.pk, test.pk),
                **push_messages.inbox_defaults(campaign, InboxNotification.PushState.SENDING),
            )
            test.inbox = inbox
            test.save(update_fields=["inbox"])
            targets.append((test, inbox, token))
        if not targets:
            raise Conflict(common.MESSAGE_NO_TEST_DEVICES)
    messages = common.campaign_messages(campaign, [(token, inbox.pk) for _test, inbox, token in targets])
    try:
        results = get_fcm_port().send(messages)
    except FcmUnavailable:
        with transaction.atomic():
            for test, inbox, _token in targets:
                CampaignTest.objects.filter(pk=test.pk).update(
                    fcm_ok=False, fcm_error=common.UNAVAILABLE_TEST
                )
                InboxNotification.objects.filter(pk=inbox.pk).update(
                    push_state=InboxNotification.PushState.FAILED, push_error=common.UNAVAILABLE_TEST
                )
        raise ProviderUnavailable(common.MESSAGE_FCM_DOWN) from None
    with transaction.atomic():
        campaign = common.lock_campaign(campaign_id)
        now = timeutils.now()
        for (test, inbox, token), result in zip(targets, results, strict=True):
            if result.ok:
                test.fcm_ok, test.provider_message_id = True, result.message_id
                inbox.push_state, inbox.pushed_at = InboxNotification.PushState.SENT, now
            else:
                test.fcm_ok, test.fcm_error = False, result.error
                inbox.push_state, inbox.push_error = InboxNotification.PushState.FAILED, result.error
                push_messages.after_failure(
                    result, token, test.device_id, topics.CAMPAIGN_SEND_BATCH, inbox.pk
                )
            test.save(update_fields=["fcm_ok", "fcm_error", "provider_message_id"])
            inbox.save(update_fields=["push_state", "pushed_at", "push_error"])
        common.audit_campaign(
            campaign,
            state.transition("test").audit_action,
            actor=actor,
            request=request,
            after={"revision": campaign.content_revision, "tests": [_test_image(test) for test in tests]},
        )
        return campaign


@transaction.atomic
def confirm_test(campaign_id: int, test_id: int, *, actor: Any, request: Any = None) -> Campaign:
    """``POST notifications/{id}/tests/{test_id}/confirm/`` (superuser): the superuser saw the test on the
    device. The test must be the current revision's (409 ``conflict`` :data:`MESSAGE_TEST_STALE`) and
    delivered (:data:`MESSAGE_TEST_UNDELIVERED`); the campaign becomes ``tested`` for that revision. A
    confirmed test answers unchanged. Audit ``notification.test_confirmed``."""
    campaign = common.lock_campaign(campaign_id)
    test = CampaignTest.objects.filter(pk=test_id, campaign=campaign).first()
    if test is None:
        raise NotFound
    if test.confirmed_at is not None and campaign.tested_revision == campaign.content_revision:
        return campaign
    state.check("confirm", campaign.state)
    if test.revision != campaign.content_revision:
        raise Conflict(common.MESSAGE_TEST_STALE)
    if test.fcm_ok is not True:
        raise Conflict(common.MESSAGE_TEST_UNDELIVERED)
    now = timeutils.now()
    before = {"state": campaign.state, "tested_revision": campaign.tested_revision}
    if test.confirmed_at is None:
        test.confirmed_at, test.confirmed_by = now, actor
        test.save(update_fields=["confirmed_at", "confirmed_by"])
    campaign.state = Campaign.State.TESTED
    campaign.tested_revision = campaign.content_revision
    campaign.test_confirmed_at, campaign.test_confirmed_by = now, actor
    campaign.save(update_fields=["state", "tested_revision", "test_confirmed_at", "test_confirmed_by"])
    common.audit_campaign(
        campaign,
        state.transition("confirm").audit_action,
        actor=actor,
        request=request,
        before=before,
        after={"state": campaign.state, "tested_revision": campaign.tested_revision, "test_id": test.pk},
    )
    return campaign


__all__ = [
    "confirm_test",
    "send_test",
]
