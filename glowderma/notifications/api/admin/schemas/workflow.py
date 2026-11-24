"""OpenAPI declarations for the campaign workflow actions: test, confirm the test, approve, return to draft,
stop and cancel."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)
from glowderma.notifications.api.admin.schemas.common import ACTION_ERRORS
from glowderma.notifications.api.admin.serializers import (
    ApproveInputSerializer,
    NotificationRecordSerializer,
    SendTestInputSerializer,
)

NOTIFICATION_TEST_POST = extend_schema(
    operation_id="admin_notifications_test",
    description=(
        "Superuser. Pushes the current revision of a `draft` / `tested` campaign to the chosen test "
        "devices (`device_ids`, 1..10 active registry ids); each push is a real inbox item of the "
        "tester and is recorded in `tests[]` with its FCM outcome. 503 `provider_unavailable` when FCM "
        "took nothing (the tests are recorded as undelivered)."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=SendTestInputSerializer,
    responses={200: NotificationRecordSerializer, **ACTION_ERRORS},
)

NOTIFICATION_TEST_CONFIRM_POST = extend_schema(
    operation_id="admin_notifications_test_confirm",
    description=(
        "Superuser: the test looked right on the device. The test must be the current revision's and "
        "delivered (409 `conflict` otherwise); the campaign becomes `tested`. Idempotent."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
)

NOTIFICATION_APPROVE_POST = extend_schema(
    operation_id="admin_notifications_approve",
    description=(
        "Superuser. A `tested` campaign becomes `scheduled` with `dispatch_after = max(now + "
        "approval_delay_minutes, scheduled_at)`; nothing is sent by this call — the beat starts the send "
        "after `dispatch_after`, and `cancel/` works until then. `expected_revision` stale → 409 "
        "`conflict`; `draft` → 409 `invalid_transition`; a repeat on the approved revision answers 200 "
        "unchanged."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=ApproveInputSerializer,
    responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
)

NOTIFICATION_RETURN_TO_DRAFT_POST = extend_schema(
    operation_id="admin_notifications_return_to_draft",
    description=(
        "Superuser. A `scheduled` (not yet started) or `cancelled` (never started) campaign goes back to "
        "`draft`: approval and test confirmation cleared, `revision` bumped, so it must be tested again."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
)

NOTIFICATION_STOP_POST = extend_schema(
    operation_id="admin_notifications_stop",
    description=(
        "Superuser; emergency stop of a `sending` campaign → `stopping`. The running batch finalises it "
        "(`partially_sent` when a push went out, else `cancelled`; remaining recipients `stopped`); "
        "pushes FCM already accepted cannot be recalled. Idempotent on `stopping` and on a campaign that "
        "ended by a stop; otherwise 409 `invalid_transition`."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
)

NOTIFICATION_CANCEL_POST = extend_schema(
    operation_id="admin_notifications_cancel",
    description="Superuser. Cancel an approved `scheduled` campaign before the beat starts it (state "
    "`cancelled`, audited `notification.cancelled`). Idempotent: an already cancelled campaign answers "
    "200 unchanged. A `draft` / `tested` campaign → 409 `invalid_transition` (delete or edit it "
    "instead); once the send started → 409 `conflict` (use `stop/`); 404 for an unknown id.",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "NOTIFICATION_APPROVE_POST",
    "NOTIFICATION_CANCEL_POST",
    "NOTIFICATION_RETURN_TO_DRAFT_POST",
    "NOTIFICATION_STOP_POST",
    "NOTIFICATION_TEST_CONFIRM_POST",
    "NOTIFICATION_TEST_POST",
]
