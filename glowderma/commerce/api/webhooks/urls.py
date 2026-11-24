"""Provider callbacks (customer contract §2.13), mounted at ``api/v1/webhooks/``."""

from django.urls import URLPattern, URLResolver, path, re_path

from glowderma.commerce.api.webhooks import bts, click, payme, uzum, yandex

urlpatterns: list[URLPattern | URLResolver] = [
    path("click/prepare/", click.ClickPrepareView.as_view()),
    path("click/complete/", click.ClickCompleteView.as_view()),
    path("payme/", payme.PaymeView.as_view()),
    path("bts/", bts.BtsWebhookView.as_view()),
    path("yandex-delivery/", yandex.YandexDeliveryWebhookView.as_view()),
    # with or without the trailing slash: Uzum's terminal is configured by hand, and APPEND_SLASH cannot
    # redirect a POST
    re_path(r"^uzum/?$", uzum.UzumWebhookView.as_view()),
]
