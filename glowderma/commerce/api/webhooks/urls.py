"""Provider callbacks (customer contract §2.13), mounted at ``api/v1/webhooks/``."""

from django.urls import URLPattern, URLResolver, path

from glowderma.commerce.api.webhooks import bts, click, payme

urlpatterns: list[URLPattern | URLResolver] = [
    path("click/prepare/", click.ClickPrepareView.as_view()),
    path("click/complete/", click.ClickCompleteView.as_view()),
    path("payme/", payme.PaymeView.as_view()),
    path("bts/", bts.BtsWebhookView.as_view()),
]
