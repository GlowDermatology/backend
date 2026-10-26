"""Customer routes of `notifications` (customer contract §2.12); mounted by ``config.urls`` at ``api/v1/``."""

from django.urls import URLPattern, URLResolver, path

from .views import AckView, InboxViewSet, ReadAllView, ReadView, UnreadCountView

urlpatterns: list[URLPattern | URLResolver] = [
    path("me/notifications/", InboxViewSet.as_view({"get": "list"}), name="customer-notifications"),
    path("me/notifications/unread-count/", UnreadCountView.as_view(), name="customer-notifications-unread"),
    path("me/notifications/read-all/", ReadAllView.as_view(), name="customer-notifications-read-all"),
    path("me/notifications/ack/", AckView.as_view(), name="customer-notifications-ack"),
    path("me/notifications/<int:pk>/read/", ReadView.as_view(), name="customer-notification-read"),
]
