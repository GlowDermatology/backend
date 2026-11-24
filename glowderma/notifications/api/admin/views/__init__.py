"""Admin views of `notifications` (contract §2.8: notifications and their approval workflow, test devices,
meta, templates, segments, members).

One subject per module: ``campaigns``, ``workflow``, ``device_registry``, ``meta``, ``status_templates``,
``segments`` and ``segment_members``; ``forms`` holds the multipart form helpers and ``common`` what they
share. Their OpenAPI declarations live in ``glowderma.notifications.api.admin.schemas``.
"""

from glowderma.notifications.api.admin.views.campaigns import (
    NotificationViewSet,
)
from glowderma.notifications.api.admin.views.device_registry import (
    TestDeviceViewSet,
)
from glowderma.notifications.api.admin.views.meta import (
    NotificationMetaView,
)
from glowderma.notifications.api.admin.views.segment_members import (
    SegmentMemberDetailView,
    SegmentMembersViewSet,
)
from glowderma.notifications.api.admin.views.segments import (
    SegmentViewSet,
)
from glowderma.notifications.api.admin.views.status_templates import (
    TemplateView,
)
from glowderma.notifications.api.admin.views.workflow import (
    NotificationApproveView,
    NotificationCancelView,
    NotificationReturnToDraftView,
    NotificationStopView,
    NotificationTestConfirmView,
    NotificationTestView,
)

__all__ = [
    "NotificationApproveView",
    "NotificationCancelView",
    "NotificationMetaView",
    "NotificationReturnToDraftView",
    "NotificationStopView",
    "NotificationTestConfirmView",
    "NotificationTestView",
    "NotificationViewSet",
    "SegmentMemberDetailView",
    "SegmentMembersViewSet",
    "SegmentViewSet",
    "TemplateView",
    "TestDeviceViewSet",
]
