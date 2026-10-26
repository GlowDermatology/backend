"""Admin routes of ``notifications`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``notifications/meta/`` is declared before nothing that could shadow it, but kept next to the list for
readability.
"""

from django.urls import URLPattern, URLResolver, path

from .views import (
    NotificationCancelView,
    NotificationMetaView,
    NotificationViewSet,
    SegmentMemberDetailView,
    SegmentMembersViewSet,
    SegmentViewSet,
    TemplateView,
)

notifications = NotificationViewSet.as_view({"get": "list", "post": "create"})
segments = SegmentViewSet.as_view({"get": "list", "post": "create"})
segment = SegmentViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
members = SegmentMembersViewSet.as_view({"get": "list", "post": "add"})

urlpatterns: list[URLPattern | URLResolver] = [
    path("notifications/", notifications, name="admin-notifications-list"),
    path("notifications/meta/", NotificationMetaView.as_view(), name="admin-notifications-meta"),
    path(
        "notifications/<int:pk>/cancel/", NotificationCancelView.as_view(), name="admin-notifications-cancel"
    ),
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
