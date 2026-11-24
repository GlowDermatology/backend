"""Writes of `notifications` (design 03 §3): campaign drafts, test pushes, approval, cancel / stop and the
batched send, status-push deliveries, templates, segments and manual members.

Delivery is at-least-once: every handler body here is idempotent at the provider boundary through the
``InboxNotification.dedupe_key`` claim protocol (§3.6) and the ``CampaignRecipient`` states (§3.5).

The package groups them by subject: ``push_messages`` (the FCM message and its profiles), ``campaigns``
(drafts, images, test devices, test pushes, the lifecycle and the batched send), ``status_pushes``, ``inbox``
(the customer inbox), ``templates`` and ``segments``. Callers import this facade.
"""

from glowderma.notifications.services.campaigns.batch_sending import (
    fail_campaign_from_dead_batch,
    finalise_stopping_campaigns,
    send_batch,
)
from glowderma.notifications.services.campaigns.common import (
    CONTENT_FIELDS,
    MESSAGE_CANCEL_DRAFT,
    MESSAGE_CANCEL_STARTED,
    MESSAGE_FCM_DOWN,
    MESSAGE_NO_TEST_DEVICES,
    MESSAGE_NOT_SENDING,
    MESSAGE_NOT_TESTED,
    MESSAGE_SEGMENT_GONE,
    MESSAGE_STALE_REVISION,
    MESSAGE_TEST_STALE,
    MESSAGE_TEST_UNDELIVERED,
    STOPPED,
    UNAVAILABLE_TEST,
    CampaignInput,
    approval_delay,
    campaign_messages,
    ensure_can_edit,
)
from glowderma.notifications.services.campaigns.drafts import (
    create_draft,
    delete_draft,
    update_draft,
)
from glowderma.notifications.services.campaigns.lifecycle import (
    approve_campaign,
    cancel_campaign,
    materialise,
    request_stop,
    return_to_draft,
    start_due_campaigns,
)
from glowderma.notifications.services.campaigns.test_devices import (
    TEST_DEVICE_MESSAGES,
    register_test_device,
    remove_test_device,
)
from glowderma.notifications.services.campaigns.test_pushes import (
    confirm_test,
    send_test,
)
from glowderma.notifications.services.inbox import (
    AckResult,
    ReadAllResult,
    ReadResult,
    acknowledge,
    mark_all_read,
    mark_read,
)
from glowderma.notifications.services.push_messages import (
    CAMPAIGN_PRIORITY,
    IMAGE_MESSAGES,
    INTERRUPTED,
    MESSAGES,
    NO_DEVICE,
    NOT_SENT_GIVE_UP,
    NOT_SENT_RETRY,
    PROVIDER_BRANDS,
    PUSH_PROFILES,
    batch_size,
    push_message,
)
from glowderma.notifications.services.segments import (
    add_members,
    create_segment,
    delete_segment,
    remove_member,
    update_segment,
)
from glowderma.notifications.services.status_pushes import (
    STILL_TRUE,
    deliver_account_security,
    deliver_appointment,
    deliver_order_status,
    order_status_title,
    render_template,
)
from glowderma.notifications.services.templates import (
    template_texts,
    update_template,
)

__all__ = [
    "CAMPAIGN_PRIORITY",
    "CONTENT_FIELDS",
    "IMAGE_MESSAGES",
    "INTERRUPTED",
    "MESSAGES",
    "MESSAGE_CANCEL_DRAFT",
    "MESSAGE_CANCEL_STARTED",
    "MESSAGE_FCM_DOWN",
    "MESSAGE_NOT_SENDING",
    "MESSAGE_NOT_TESTED",
    "MESSAGE_NO_TEST_DEVICES",
    "MESSAGE_SEGMENT_GONE",
    "MESSAGE_STALE_REVISION",
    "MESSAGE_TEST_STALE",
    "MESSAGE_TEST_UNDELIVERED",
    "NOT_SENT_GIVE_UP",
    "NOT_SENT_RETRY",
    "NO_DEVICE",
    "PROVIDER_BRANDS",
    "PUSH_PROFILES",
    "STILL_TRUE",
    "STOPPED",
    "TEST_DEVICE_MESSAGES",
    "UNAVAILABLE_TEST",
    "AckResult",
    "CampaignInput",
    "ReadAllResult",
    "ReadResult",
    "acknowledge",
    "add_members",
    "approval_delay",
    "approve_campaign",
    "batch_size",
    "campaign_messages",
    "cancel_campaign",
    "confirm_test",
    "create_draft",
    "create_segment",
    "delete_draft",
    "delete_segment",
    "deliver_account_security",
    "deliver_appointment",
    "deliver_order_status",
    "ensure_can_edit",
    "fail_campaign_from_dead_batch",
    "finalise_stopping_campaigns",
    "mark_all_read",
    "mark_read",
    "materialise",
    "order_status_title",
    "push_message",
    "register_test_device",
    "remove_member",
    "remove_test_device",
    "render_template",
    "request_stop",
    "return_to_draft",
    "send_batch",
    "send_test",
    "start_due_campaigns",
    "template_texts",
    "update_draft",
    "update_segment",
    "update_template",
]
