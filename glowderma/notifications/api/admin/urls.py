"""Admin routes of ``notifications`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``notifications/meta/`` and ``notifications/test-devices/`` sit before ``notifications/<int:pk>/`` (the int
converter never matches them anyway) and are kept next to the list for readability.
"""

from django.urls import URLPattern, URLResolver, path

from .views import (
    NotificationApproveView,
    NotificationCancelView,
    NotificationMetaView,
    NotificationReturnToDraftView,
    NotificationStopView,
    NotificationTestConfirmView,
    NotificationTestView,
    NotificationViewSet,
    SegmentMemberDetailView,
    SegmentMembersViewSet,
    SegmentViewSet,
    TemplateView,
    TestDeviceViewSet,
)

notifications = NotificationViewSet.as_view({"get": "list", "post": "create"})
notification = NotificationViewSet.as_view(
    {"get": "retrieve", "patch": "partial_update", "delete": "destroy"}
)
test_devices = TestDeviceViewSet.as_view({"get": "list", "post": "create"})
test_device = TestDeviceViewSet.as_view({"delete": "destroy"})
segments = SegmentViewSet.as_view({"get": "list", "post": "create"})
segment = SegmentViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
members = SegmentMembersViewSet.as_view({"get": "list", "post": "add"})

urlpatterns: list[URLPattern | URLResolver] = [
    path("notifications/", notifications, name="admin-notifications-list"),
    path("notifications/meta/", NotificationMetaView.as_view(), name="admin-notifications-meta"),
    path("notifications/test-devices/", test_devices, name="admin-notification-test-devices"),
    path("notifications/test-devices/<int:pk>/", test_device, name="admin-notification-test-device-detail"),
    path("notifications/<int:pk>/", notification, name="admin-notifications-detail"),
    path("notifications/<int:pk>/test/", NotificationTestView.as_view(), name="admin-notifications-test"),
    path(
        "notifications/<int:pk>/tests/<int:test_id>/confirm/",
        NotificationTestConfirmView.as_view(),
        name="admin-notifications-test-confirm",
    ),
    path(
        "notifications/<int:pk>/approve/",
        NotificationApproveView.as_view(),
        name="admin-notifications-approve",
    ),
    path(
        "notifications/<int:pk>/return-to-draft/",
        NotificationReturnToDraftView.as_view(),
        name="admin-notifications-return-to-draft",
    ),
    path(
        "notifications/<int:pk>/cancel/", NotificationCancelView.as_view(), name="admin-notifications-cancel"
    ),
    path("notifications/<int:pk>/stop/", NotificationStopView.as_view(), name="admin-notifications-stop"),
    path("notification-templates/<str:status>/", TemplateView.as_view(), name="admin-notification-templates"),
    path("segments/", segments, name="admin-segments-list"),
    path("segments/<int:pk>/", segment, name="admin-segments-detail"),
    path("segments/<int:pk>/members/", members, name="admin-segment-members"),
    path(
        "segments/<int:pk>/members/<int:customer_id>/",
        SegmentMemberDetailView.as_view(),
        name="admin-segment-member-detail",
    ),
]
