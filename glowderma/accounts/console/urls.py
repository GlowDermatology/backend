"""Console sign-in routes, mounted by ``config.urls`` before ``admin.site.urls``: they take precedence over
the site's own ``login/`` and ``logout/`` (``reverse("admin:login")`` still resolves to the same paths)."""

from django.urls import URLPattern, path

from glowderma.accounts.console import views

urlpatterns: list[URLPattern] = [
    path("django-admin/login/", views.GuardedAdminLoginView.as_view(), name="ops_login"),
    path("django-admin/logout/", views.ConsoleLogoutView.as_view(), name="ops_logout"),
    path("django-admin/2fa/", views.TotpVerifyView.as_view(), name="ops_2fa_verify"),
    path("django-admin/2fa/recovery/", views.RecoveryCodeSignInView.as_view(), name="ops_2fa_recovery"),
    path("django-admin/2fa/enrol/", views.EnrolmentView.as_view(), name="ops_2fa_enrol"),
    path(
        "django-admin/2fa/recovery-codes/", views.RecoveryCodesView.as_view(), name="ops_2fa_recovery_codes"
    ),
    path("django-admin/2fa/cancel/", views.CancelSignInView.as_view(), name="ops_2fa_cancel"),
]
