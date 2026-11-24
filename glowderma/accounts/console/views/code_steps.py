"""What the console's code steps (TOTP, recovery code, enrolment) share: the pending sign-in they require, the
``django_admin_otp`` lockout plus the per-address budget, and the 429 page of a running lockout."""

from __future__ import annotations

import logging
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseBase, HttpResponseRedirect
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import View
from rest_framework.exceptions import Throttled

from glowderma.accounts import console_sessions
from glowderma.accounts.console import events, lockouts, notices, paths
from glowderma.accounts.console.views import rendering
from glowderma.accounts.models import StaffUser
from glowderma.accounts.security.address_budget import IpFailureBudget, IpThrottled, client_bucket
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.core import alerts

logger = logging.getLogger("glowderma.accounts.console.views")

SCOPE = "django_admin_otp"
CODE_STEP = "code"  # the per-address budget of the code steps (TOTP, recovery code, enrolment)


@method_decorator([sensitive_post_parameters("code"), csrf_protect, never_cache], name="dispatch")
class SignInStepView(View):
    """A step of a pending sign-in (GET shows it, POST submits its code): without one (none, expired, password
    changed) back to the login page. Subclasses implement :meth:`step`."""

    http_method_names = ["get", "post"]

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if (request.method or "").lower() not in self.http_method_names:
            return self.http_method_not_allowed(request, *args, **kwargs)
        had_signin = console_sessions.PENDING in request.session
        current = console_sessions.pending(request)
        if current is None:
            if had_signin:
                messages.warning(request, notices.SIGNIN_EXPIRED)
            return HttpResponseRedirect(paths.LOGIN_PATH)
        return self.step(request, current)

    def step(self, request: HttpRequest, current: console_sessions.Pending) -> HttpResponse:
        raise NotImplementedError


def locked_response(
    request: HttpRequest, user: StaffUser, exc: Throttled, template: str, context: dict[str, Any]
) -> HttpResponse:
    message, wait = notices.lockout_message(exc)
    if isinstance(exc, IpThrottled):
        lockouts.record_ip_locked(request, CODE_STEP, wait, email=user.email)
    else:
        events.record("console.otp_locked", request, user, wait=wait)
        # The password was right but the codes were not: a stolen password, or a lost phone. Logged and
        # alerted (one alert per account per ALERT_DEDUPE_MINUTES window).
        logger.warning("console.otp_locked staff_id=%s wait=%s", user.pk, wait)
        alerts.alert("console.otp_locked", {"staff_id": user.pk, "wait": wait}, scope=str(user.pk))
    clear_code(context.get("form"))
    response = rendering.render_step(request, template, {**context, "lockout_message": message}, status=429)
    response["Retry-After"] = str(wait)
    return response


def reserve_attempt(request: HttpRequest, user: StaffUser) -> Throttled | None:
    """Reserve one code attempt for the client address and for ``user`` (``None``), or the running lockout
    (``IpThrottled`` for the address)."""
    try:
        IpFailureBudget(CODE_STEP).reserve(client_bucket(request))
        LoginGuard().check(SCOPE, email_hash(user.email))
    except Throttled as exc:
        return exc
    return None


def record_failed_attempt(request: HttpRequest, user: StaffUser, method: str, form: Any) -> None:
    LoginGuard().record_failure(SCOPE, email_hash(user.email))
    events.record("console.otp_failed", request, user, method=method)
    clear_code(form)


def clear_code(form: Any) -> None:
    """Re-render the code field empty after a failed attempt (its errors stay): the old code is useless."""
    if form is not None and form.is_bound:
        data = form.data.copy()
        data["code"] = ""
        form.data = data


def record_success(request: HttpRequest, user: StaffUser) -> None:
    LoginGuard().reset(SCOPE, email_hash(user.email))
    IpFailureBudget(CODE_STEP).refund(client_bucket(request))


__all__ = [
    "CODE_STEP",
    "SCOPE",
    "SignInStepView",
    "clear_code",
    "locked_response",
    "record_failed_attempt",
    "record_success",
    "reserve_attempt",
]
