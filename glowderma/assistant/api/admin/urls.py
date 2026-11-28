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
    path("scenes/status/", views.ScenesStatusView.as_view(), name="admin-scenes-status"),
    path("scenes/templates/", views.SceneTemplatesView.as_view(), name="admin-scene-templates"),
    path(
        "products/<int:product_id>/images/<int:image_id>/scenes/",
        views.SceneJobStartView.as_view(),
        name="admin-scene-jobs-start",
    ),
    path("scenes/jobs/<int:pk>/", views.SceneJobDetailView.as_view(), name="admin-scene-job"),
    path("scenes/jobs/<int:pk>/accept/", views.SceneJobAcceptView.as_view(), name="admin-scene-job-accept"),
    path(
        "scenes/jobs/<int:pk>/template/",
        views.SceneJobTemplateView.as_view(),
        name="admin-scene-job-template",
    ),
    path(
        "scenes/jobs/<int:pk>/discard/", views.SceneJobDiscardView.as_view(), name="admin-scene-job-discard"
    ),
    path("scenes/banner-images/", views.BannerImageStartView.as_view(), name="admin-banner-images-start"),
    path("scenes/jobs/<int:pk>/banner/", views.BannerImageUseView.as_view(), name="admin-scene-job-banner"),
]
