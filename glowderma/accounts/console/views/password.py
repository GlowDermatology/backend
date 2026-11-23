"""The console's first step: the guarded Django-admin login (password, then the second factor), with its
lockout page."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import LoginView
from django.core.exceptions import NON_FIELD_ERRORS
from django.http import HttpRequest, HttpResponse, HttpResponseBase, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext
from rest_framework.exceptions import Throttled

from glowderma.accounts import console_sessions, second_factor
from glowderma.accounts.console import events, forms, lockouts, notices, paths
from glowderma.accounts.security.address_budget import IpThrottled


class ConsolePasswordView(LoginView):
    """The console's first step. A correct password logs nobody in while the second factor is required
    (``OPS_REQUIRE_2FA``): it records a pending sign-in and sends the browser to the code step, or to the
    enrolment step when the user has no confirmed TOTP device yet (``glowderma.accounts.console``)."""

    def form_valid(self, form: Any) -> HttpResponse:
        user = form.get_user()
        next_url = console_sessions.safe_next(self.request, self.get_redirect_url() or None)
        if not settings.OPS_REQUIRE_2FA:
            console_sessions.complete_login(self.request, user, None)
            events.record("console.login_succeeded", self.request, user, method="password")
            return HttpResponseRedirect(next_url)
        console_sessions.start_pending(self.request, user, next_url)
        events.record("console.password_verified", self.request, user)
        step = (
            "ops_2fa_verify"
            if second_factor.has_second_factor(user, scope=second_factor.CONSOLE)
            else "ops_2fa_enrol"
        )
        return HttpResponseRedirect(reverse(step))

    def form_invalid(self, form: Any) -> HttpResponse:
        email = str(form.data.get("username", "")).strip().lower()[:254]
        pending = form.has_error(NON_FIELD_ERRORS, forms.PASSWORD_CHANGE_REQUIRED_CODE)
        events.record(
            "console.login_failed",
            self.request,
            email=email,
            reason=forms.PASSWORD_CHANGE_REQUIRED_CODE if pending else "",
        )
        return super().form_invalid(form)


def _login_context(request: HttpRequest, extra_context: dict[str, Any] | None) -> dict[str, Any]:
    """What ``AdminSite.login`` hands the template, so Unfold's login page renders unchanged."""
    context: dict[str, Any] = {
        **admin.site.each_context(request),
        "title": gettext("Kirish"),
        "subtitle": None,
        "app_path": request.get_full_path(),
        "username": "",
    }
    if REDIRECT_FIELD_NAME not in request.GET and REDIRECT_FIELD_NAME not in request.POST:
        context[REDIRECT_FIELD_NAME] = reverse("admin:index", current_app=admin.site.name)
    context.update(extra_context or {})
    return context


def guarded_admin_login(
    request: HttpRequest, extra_context: dict[str, Any] | None = None
) -> HttpResponseBase:
    request.current_app = admin.site.name
    if request.method == "GET" and admin.site.has_permission(request):
        return HttpResponseRedirect(reverse("admin:index", current_app=admin.site.name))
    context = _login_context(request, extra_context)
    try:
        return ConsolePasswordView.as_view(
            extra_context=context,
            authentication_form=admin.site.login_form,
            template_name=paths.LOGIN_TEMPLATE,
        )(request)
    except Throttled as exc:
        message, wait = notices.lockout_message(exc)
        email = str(request.POST.get("username", "")).strip().lower()[:254]
        if isinstance(exc, IpThrottled):
            lockouts.record_ip_locked(request, "password", wait, email=email)
        else:
            events.record("console.login_locked", request, email=email, wait=wait)
        form = forms.GuardedAdminAuthenticationForm(request, initial={"username": email})
        response = TemplateResponse(
            request, paths.LOGIN_TEMPLATE, {**context, "form": form, "lockout_message": message}, status=429
        )
        response["Retry-After"] = str(wait)
        add_never_cache_headers(response)
        return response


__all__ = [
    "ConsolePasswordView",
    "guarded_admin_login",
]
