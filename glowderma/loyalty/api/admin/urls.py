"""Admin routes of ``loyalty`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
"""

from django.urls import URLPattern, URLResolver, path

from .views import CashbackPolicyView, PolicyVersionCreateView

urlpatterns: list[URLPattern | URLResolver] = [
    path("cashback/policy/", CashbackPolicyView.as_view(), name="admin-cashback-policy"),
    path(
        "cashback/policy-versions/", PolicyVersionCreateView.as_view(), name="admin-cashback-policy-versions"
    ),
]
