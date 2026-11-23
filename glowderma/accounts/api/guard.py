"""The ops console's password step: the guarded Django-admin login form and view, and the audit of an
address over its failure budget. The lockout primitives live in ``glowderma.accounts.security``."""

import logging
import math
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import LoginView
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext, ngettext
from rest_framework.exceptions import Throttled
from unfold.forms import AuthenticationForm as UnfoldAuthenticationForm

from glowderma.accounts import console_sessions, second_factor
from glowderma.accounts.console import events
from glowderma.accounts.security.address_budget import IpFailureBudget, IpThrottled, client_bucket
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.core import alerts

logger = logging.getLogger(__name__)


def record_ip_locked(request: HttpRequest, step: str, wait: int, email: str = "") -> None:
    """Audit and alert an address over its budget (the address is in the audit row, never in the alert)."""
    events.record("console.ip_locked", request, email=email, step=step, wait=wait)
    logger.warning("console.ip_locked step=%s wait=%s", step, wait)
    alerts.alert("console.ip_locked", {"step": step, "wait": wait}, scope=step)


# Unfold's admin login form: a ``django.contrib.admin.forms.AdminAuthenticationForm`` with styled widgets.
if TYPE_CHECKING:
    _AdminAuthenticationForm = AdminAuthenticationForm
else:
    _AdminAuthenticationForm = UnfoldAuthenticationForm


PASSWORD_CHANGE_REQUIRED_CODE = "password_change_required"  # noqa: S105 - an error code


def console_password_change_required() -> str:
    """The console login refusal of an account with a pending forced password change (its language)."""
    return gettext("Avval admin panelda o‘z parolingizni o‘rnating, so‘ng konsolga qayta kiring.")


class GuardedAdminAuthenticationForm(_AdminAuthenticationForm):
    def clean(self) -> dict[str, Any]:
        username = str(self.cleaned_data.get("username", "")).strip().lower()
        self.cleaned_data["username"] = username
        budget = IpFailureBudget("password")
        bucket = client_bucket(self.request) if self.request is not None else ""
        if bucket:
            budget.reserve(bucket)
        guard = LoginGuard()
        hashed = email_hash(username)
        guard.check("django_admin_login", hashed)
        try:
            result = super().clean()
        except ValidationError:
            guard.record_failure("django_admin_login", hashed)
            raise
        guard.reset("django_admin_login", hashed)
        if bucket:
            budget.refund(bucket)
        user = self.get_user()
        if user is not None and getattr(user, "must_change_password", False):
            # The right password, but someone else chose it (feat/admin-ux): the person sets their own in the
            # admin panel first. Not a failed guess (the lockout and the address budget stay as they are).
            raise ValidationError(console_password_change_required(), code=PASSWORD_CHANGE_REQUIRED_CODE)
        return result


LOGIN_TEMPLATE = "accounts/console/login.html"


def lockout_message(exc: Throttled) -> tuple[str, int]:
    """The lockout notice in the console's language, and the wait in seconds."""
    wait = int(math.ceil(float(getattr(exc, "wait", None) or 0)))
    notice = ngettext(
        "Juda ko‘p urinish. %(wait)s soniyadan so‘ng qayta urinib ko‘ring.",
        "Juda ko‘p urinish. %(wait)s soniyadan so‘ng qayta urinib ko‘ring.",
        wait,
    ) % {"wait": wait}
    return notice, wait


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
        pending = form.has_error(NON_FIELD_ERRORS, PASSWORD_CHANGE_REQUIRED_CODE)
        events.record(
            "console.login_failed",
            self.request,
            email=email,
            reason=PASSWORD_CHANGE_REQUIRED_CODE if pending else "",
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
            template_name=LOGIN_TEMPLATE,
        )(request)
    except Throttled as exc:
        message, wait = lockout_message(exc)
        email = str(request.POST.get("username", "")).strip().lower()[:254]
        if isinstance(exc, IpThrottled):
            record_ip_locked(request, "password", wait, email=email)
        else:
            events.record("console.login_locked", request, email=email, wait=wait)
        form = GuardedAdminAuthenticationForm(request, initial={"username": email})
        response = TemplateResponse(
            request, LOGIN_TEMPLATE, {**context, "form": form, "lockout_message": message}, status=429
        )
        response["Retry-After"] = str(wait)
        add_never_cache_headers(response)
        return response
