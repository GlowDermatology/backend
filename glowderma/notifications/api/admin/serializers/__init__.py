"""Admin serializers of `notifications` (contract §2.8: NotificationRecord, meta, templates, segments), one
subject per module: ``records`` (the NotificationRecord), ``drafts`` (the multipart create and the PATCH
meta), ``workflow`` (test and approve), ``device_registry`` (test devices), ``status_templates``, ``meta``,
``segments`` and ``customer_list_items``; ``messages`` holds the refusal texts.
"""

from glowderma.notifications.api.admin.serializers.customer_list_items import (
    CustomerListItemSerializer,
)
from glowderma.notifications.api.admin.serializers.device_registry import (
    TEST_DEVICES_MAX,
    TestDeviceCreateSerializer,
    TestDeviceSerializer,
)
from glowderma.notifications.api.admin.serializers.drafts import (
    NotificationAudienceInputSerializer,
    NotificationCreateSerializer,
    NotificationLinkInputSerializer,
    NotificationListQuerySerializer,
    NotificationPatchMetaSerializer,
)
from glowderma.notifications.api.admin.serializers.messages import (
    MESSAGES,
    UNKNOWN_PLACEHOLDER,
)
from glowderma.notifications.api.admin.serializers.meta import (
    LinkOptionSerializer,
    MetaSegmentSerializer,
    NotificationMetaSerializer,
)
from glowderma.notifications.api.admin.serializers.records import (
    CampaignTestSerializer,
    NotificationAudienceSerializer,
    NotificationLinkSerializer,
    NotificationRecordSerializer,
    record_audience,
    record_link,
)
from glowderma.notifications.api.admin.serializers.segments import (
    MEMBERS_MAX_ITEMS,
    CustomerIdsField,
    SegmentMembersInputSerializer,
    SegmentRulesSerializer,
    SegmentSerializer,
    SegmentWriteSerializer,
)
from glowderma.notifications.api.admin.serializers.status_templates import (
    StatusTemplateSerializer,
    TemplateTextsSerializer,
    TemplateUpdateSerializer,
)
from glowderma.notifications.api.admin.serializers.workflow import (
    ApproveInputSerializer,
    SendTestInputSerializer,
)

__all__ = [
    "MEMBERS_MAX_ITEMS",
    "MESSAGES",
    "TEST_DEVICES_MAX",
    "UNKNOWN_PLACEHOLDER",
    "ApproveInputSerializer",
    "CampaignTestSerializer",
    "CustomerIdsField",
    "CustomerListItemSerializer",
    "LinkOptionSerializer",
    "MetaSegmentSerializer",
    "NotificationAudienceInputSerializer",
    "NotificationAudienceSerializer",
    "NotificationCreateSerializer",
    "NotificationLinkInputSerializer",
    "NotificationLinkSerializer",
    "NotificationListQuerySerializer",
    "NotificationMetaSerializer",
    "NotificationPatchMetaSerializer",
    "NotificationRecordSerializer",
    "SegmentMembersInputSerializer",
    "SegmentRulesSerializer",
    "SegmentSerializer",
    "SegmentWriteSerializer",
    "SendTestInputSerializer",
    "StatusTemplateSerializer",
    "TemplateTextsSerializer",
    "TemplateUpdateSerializer",
    "TestDeviceCreateSerializer",
    "TestDeviceSerializer",
    "record_audience",
    "record_link",
]
