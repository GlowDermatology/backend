"""Console sign-in routes, mounted by ``config.urls`` before ``admin.site.urls``: they take precedence over
the site's own ``login/``, ``logout/`` and ``password_change/`` (``reverse("admin:login")`` and the others
still resolve to the same paths)."""

from django.urls import URLPattern, path

from glowderma.accounts.api.guard import guarded_admin_login
from glowderma.accounts.console import views

urlpatterns: list[URLPattern] = [
    path("django-admin/login/", guarded_admin_login, name="ops_login"),
    path("django-admin/logout/", views.logout, name="ops_logout"),
    path("django-admin/2fa/", views.verify, name="ops_2fa_verify"),
    path("django-admin/2fa/recovery/", views.recovery, name="ops_2fa_recovery"),
    path("django-admin/2fa/enrol/", views.enrol, name="ops_2fa_enrol"),
    path("django-admin/2fa/cancel/", views.cancel, name="ops_2fa_cancel"),
    path("django-admin/password_change/", views.password_change, name="ops_password_change"),
]
