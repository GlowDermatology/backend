"""Admin routes of ``assistant`` (mounted under ``api/v1/admin/`` by ``config.urls``); every name
``admin-``."""

from django.urls import URLPattern, URLResolver, path

from glowderma.assistant.api.admin import views

urlpatterns: list[URLPattern | URLResolver] = [
    path("assistant/status/", views.AssistantStatusView.as_view(), name="admin-assistant-status"),
    path(
        "products/<int:product_id>/assistant/jobs/",
        views.AssistantJobStartView.as_view(),
        name="admin-assistant-jobs-start",
    ),
    path("assistant/jobs/<int:pk>/", views.AssistantJobDetailView.as_view(), name="admin-assistant-job"),
    path(
        "assistant/jobs/<int:pk>/accept/",
        views.AssistantJobAcceptView.as_view(),
        name="admin-assistant-job-accept",
    ),
    path(
        "assistant/jobs/<int:pk>/apply/",
        views.AssistantJobApplyView.as_view(),
        name="admin-assistant-job-apply",
    ),
    path(
        "assistant/jobs/<int:pk>/discard/",
        views.AssistantJobDiscardView.as_view(),
        name="admin-assistant-job-discard",
    ),
]
