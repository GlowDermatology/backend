"""The console's second step and its exits, Unfold-styled like the login page.

``verify`` (TOTP code), ``recovery`` (one-time recovery code) and ``enrol`` (first TOTP device: QR + manual
key, confirmed by a code, then the ten recovery codes shown once) all need a pending sign-in from the password
step and share the ``django_admin_otp`` lockout (LoginGuard: five failures a minute lock the user out for
1, 2, 4, 8, then 15 minutes) plus a per-address budget (``guard.IpFailureBudget``). ``cancel`` drops a
pending sign-in, ``logout`` ends a console session.

Enrolment needs an open enrolment window (``access.enrolment_window``: a grant or a 2FA reset opened it, the
same manager did not also reset the password); otherwise the pending sign-in ends with a message to ask a
manager. Its recovery codes are shown by a separate GET page (``recovery_codes``: POST → redirect → GET),
exactly once from the session, so a reload never re-posts the enrolment form.

``csrf_failure`` is ``CSRF_FAILURE_VIEW``: the console's Uzbek 403 page instead of Django's English one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from django.contrib import admin, auth, messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.exceptions import Throttled

from glowderma.accounts.api.guard import (
    IpFailureBudget,
    IpThrottled,
    LoginGuard,
    client_bucket,
    email_hash,
    lockout_message,
    record_ip_locked,
)
from glowderma.accounts.console import access, events, state, twofactor
from glowderma.accounts.console.forms import RecoveryCodeForm, TotpCodeForm
from glowderma.accounts.models import StaffUser
from glowderma.core import alerts
from glowderma.core.network import CONSOLE_PREFIX

logger = logging.getLogger(__name__)

SCOPE = "django_admin_otp"
CODE_STEP = "code"  # the per-address budget of the code steps (TOTP, recovery code, enrolment)
LOGIN_PATH = "/django-admin/login/"
WRONG_CODE = "Kod noto‘g‘ri yoki allaqachon ishlatilgan. Ilovadagi yangi kodni kiriting."
WRONG_RECOVERY = "Zaxira kod noto‘g‘ri yoki allaqachon ishlatilgan."
SIGNIN_EXPIRED = "Kirish muddati tugadi. Email va parolni qaytadan kiriting."
FEW_RECOVERY_CODES = (
    "Zaxira kodlardan {left} tasi qoldi. Menejerdan 2FA ni qayta o‘rnatishni so‘rang (yangi kodlar beriladi)."
)
LOGGED_OUT = "Siz konsoldan chiqdingiz."
ENROL_CLOSED = (
    "Ikki bosqichli himoyani sozlash muddati tugagan yoki ochilmagan. Menejerdan 2FA ni qayta o‘rnatishni "
    "so‘rang, so‘ng qaytadan kiring."
)
ENROL_SAME_MANAGER = (
    "Parolingiz va 2FA bir menejer tomonidan tiklangan: xavfsizlik uchun 2FA ni boshqa menejer (yoki server "
    "buyrug‘i) qayta o‘rnatishi kerak."
)
CODES_SHOWN_ONCE = (
    "Zaxira kodlar faqat bir marta ko‘rsatiladi va ular endi ko‘rsatilmaydi. Kodlarni saqlamagan bo‘lsangiz, "
    "menejerdan 2FA ni qayta o‘rnatishni so‘rang."
)
CSRF_FAILED = (
    "Sahifa eskirgan yoki sessiya yangilangan, shuning uchun so‘rov qabul qilinmadi. Sahifani qaytadan "
    "oching va yana urinib ko‘ring."
)
CODES_FILENAME = "glowderma-ops-zaxira-kodlar.txt"

type StepView = Callable[[HttpRequest, state.Pending], HttpResponse]


def _context(request: HttpRequest, title: str, **extra: Any) -> dict[str, Any]:
    request.current_app = admin.site.name
    return {**admin.site.each_context(request), "title": title, "subtitle": None, **extra}


def _render(request: HttpRequest, template: str, context: dict[str, Any], status: int = 200) -> HttpResponse:
    response = TemplateResponse(request, template, context, status=status)
    add_never_cache_headers(response)
    return response


def signin_step(view: StepView) -> Callable[[HttpRequest], HttpResponse]:
    """A step of a pending sign-in: without one (none, expired, password changed) back to the login page."""

    @sensitive_post_parameters("code")
    @csrf_protect
    @never_cache
    @require_http_methods(["GET", "POST"])
    def wrapper(request: HttpRequest) -> HttpResponse:
        had_signin = state.PENDING in request.session
        current = state.pending(request)
        if current is None:
            if had_signin:
                messages.warning(request, SIGNIN_EXPIRED)
            return HttpResponseRedirect(LOGIN_PATH)
        return view(request, current)

    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _locked(
    request: HttpRequest, user: StaffUser, exc: Throttled, template: str, context: dict[str, Any]
) -> HttpResponse:
    message, wait = lockout_message(exc)
    if isinstance(exc, IpThrottled):
        record_ip_locked(request, CODE_STEP, wait, email=user.email)
    else:
        events.record("console.otp_locked", request, user, wait=wait)
        # The password was right but the codes were not: a stolen password, or a lost phone. Logged and
        # alerted (one alert per account per ALERT_DEDUPE_MINUTES window).
        logger.warning("console.otp_locked staff_id=%s wait=%s", user.pk, wait)
        alerts.alert("console.otp_locked", {"staff_id": user.pk, "wait": wait}, scope=str(user.pk))
    _clear_code(context.get("form"))
    response = _render(request, template, {**context, "lockout_message": message}, status=429)
    response["Retry-After"] = str(wait)
    return response


def _attempt(request: HttpRequest, user: StaffUser) -> Throttled | None:
    """Reserve one code attempt for the client address and for ``user`` (``None``), or the running lockout
    (``IpThrottled`` for the address)."""
    try:
        IpFailureBudget(CODE_STEP).reserve(client_bucket(request))
        LoginGuard().check(SCOPE, email_hash(user.email))
    except Throttled as exc:
        return exc
    return None


def _failed(request: HttpRequest, user: StaffUser, method: str, form: Any) -> None:
    LoginGuard().record_failure(SCOPE, email_hash(user.email))
    events.record("console.otp_failed", request, user, method=method)
    _clear_code(form)


def _clear_code(form: Any) -> None:
    """Re-render the code field empty after a failed attempt (its errors stay): the old code is useless."""
    if form is not None and form.is_bound:
        data = form.data.copy()
        data["code"] = ""
        form.data = data


def _succeeded(request: HttpRequest, user: StaffUser) -> None:
    LoginGuard().reset(SCOPE, email_hash(user.email))
    IpFailureBudget(CODE_STEP).refund(client_bucket(request))


@signin_step
def verify(request: HttpRequest, current: state.Pending) -> HttpResponse:
    """The TOTP code of the user's confirmed device."""
    user = current.user
    if not twofactor.has_second_factor(user):
        return HttpResponseRedirect(reverse("ops_2fa_enrol"))
    form = TotpCodeForm(request.POST if request.method == "POST" else None)
    template = "accounts/console/verify.html"
    context = _context(request, "Ikki bosqichli tekshiruv", form=form, email=user.email)
    if request.method == "POST":
        if (locked := _attempt(request, user)) is not None:
            return _locked(request, user, locked, template, context)
        device = twofactor.verify_totp(user, form.cleaned_data["code"]) if form.is_valid() else None
        if device is not None:
            _succeeded(request, user)
            state.complete_login(request, user, device)
            events.record("console.login_succeeded", request, user, method="totp")
            return HttpResponseRedirect(state.safe_next(request, current.next_url))
        _failed(request, user, "totp", form)
        if form.is_valid():
            form.add_error(None, WRONG_CODE)
    return _render(request, template, context)


@signin_step
def recovery(request: HttpRequest, current: state.Pending) -> HttpResponse:
    """One of the user's recovery codes instead of the TOTP code (the code is used up)."""
    user = current.user
    if not twofactor.has_second_factor(user):
        return HttpResponseRedirect(reverse("ops_2fa_enrol"))
    form = RecoveryCodeForm(request.POST if request.method == "POST" else None)
    template = "accounts/console/recovery.html"
    context = _context(request, "Zaxira kod bilan kirish", form=form, email=user.email)
    if request.method == "POST":
        if (locked := _attempt(request, user)) is not None:
            return _locked(request, user, locked, template, context)
        used = twofactor.use_recovery_code(user, form.cleaned_data["code"]) if form.is_valid() else None
        if used is not None:
            _succeeded(request, user)
            state.complete_login(request, user, used.device)
            events.record("console.recovery_code_used", request, user, remaining=used.remaining)
            events.record("console.login_succeeded", request, user, method="recovery")
            if used.remaining <= 3:
                messages.warning(request, FEW_RECOVERY_CODES.format(left=used.remaining))
            return HttpResponseRedirect(state.safe_next(request, current.next_url))
        _failed(request, user, "recovery", form)
        if form.is_valid():
            form.add_error(None, WRONG_RECOVERY)
    return _render(request, template, context)


def _refuse_enrolment(request: HttpRequest, user: StaffUser, reason: str) -> HttpResponse:
    """No enrolment without an open window (module doc): the pending sign-in ends, the login page says why."""
    TOTPDevice.objects.filter(user=user, confirmed=False).delete()
    state.clear_pending(request)
    events.record("console.enrol_refused", request, user, reason=reason)
    messages.error(request, ENROL_SAME_MANAGER if reason == access.SAME_MANAGER else ENROL_CLOSED)
    return HttpResponseRedirect(LOGIN_PATH)


@signin_step
def enrol(request: HttpRequest, current: state.Pending) -> HttpResponse:
    """First sign-in without a TOTP device, inside an open enrolment window: scan the QR code (or type the
    key), confirm with a code; the recovery codes follow on ``recovery_codes`` (shown once, never stored in
    plain text beyond that one redirect)."""
    user = current.user
    if twofactor.has_second_factor(user):
        return HttpResponseRedirect(reverse("ops_2fa_verify"))
    window = access.enrolment_window(user)
    refusal = access.enrolment_refusal(user, window)
    if refusal or window is None:
        return _refuse_enrolment(request, user, refusal or access.NO_WINDOW)
    device = twofactor.enrolment_device(user, request.session.get(state.ENROLMENT_DEVICE))
    request.session[state.ENROLMENT_DEVICE] = device.pk
    form = TotpCodeForm(request.POST if request.method == "POST" else None)
    template = "accounts/console/enrol.html"
    context = _context(
        request,
        "Ikki bosqichli himoyani yoqish",
        form=form,
        email=user.email,
        qr_svg=twofactor.qr_svg(device),
        manual_key=twofactor.manual_key(device),
    )
    if request.method == "POST":
        if (locked := _attempt(request, user)) is not None:
            return _locked(request, user, locked, template, context)
        enrolment = (
            twofactor.confirm_enrolment(user, device.pk, form.cleaned_data["code"])
            if form.is_valid()
            else None
        )
        if enrolment is not None:
            _succeeded(request, user)
            state.complete_login(request, user, enrolment.device)
            events.record(
                "console.2fa_enrolled",
                request,
                user,
                recovery_codes=len(enrolment.recovery_codes),
                window_audit_id=window.audit_id,
                window_action=window.action,
                window_opened_by=window.opened_by or "system",
                window_via=window.via,
            )
            events.record("console.login_succeeded", request, user, method="enrolment")
            logger.warning("console.2fa_enrolled staff_id=%s window_audit_id=%s", user.pk, window.audit_id)
            alerts.alert(
                "console.2fa_enrolled",
                {"staff_id": user.pk, "window_audit_id": window.audit_id, "via": window.via},
                scope=str(user.pk),
            )
            state.keep_recovery_codes(
                request, enrolment.recovery_codes, state.safe_next(request, current.next_url)
            )
            return HttpResponseRedirect(reverse("ops_2fa_recovery_codes"))
        _failed(request, user, "enrolment", form)
        if form.is_valid():
            form.add_error(None, WRONG_CODE)
    return _render(request, template, context)


@never_cache
@require_GET
def recovery_codes(request: HttpRequest) -> HttpResponse:
    """The recovery codes of the enrolment that just finished, exactly once (they leave the session on this
    request); afterwards the page only says they were shown once. A verified console session only."""
    request.current_app = admin.site.name
    if not admin.site.has_permission(request):
        return HttpResponseRedirect(LOGIN_PATH)
    kept = state.take_recovery_codes(request)
    if kept is None:
        context = _context(
            request, "Zaxira kodlar", shown_once=CODES_SHOWN_ONCE, next_url=state.CONSOLE_INDEX
        )
        return _render(request, "accounts/console/recovery_codes.html", context)
    codes, next_url = kept
    text = "Glow Derma Ops — zaxira kodlar\n" + "\n".join(codes) + "\n"
    context = _context(
        request,
        "Zaxira kodlar",
        recovery_codes=codes,
        next_url=next_url,
        email=getattr(request.user, "email", ""),
        codes_download=f"data:text/plain;charset=utf-8,{quote(text)}",
        codes_filename=CODES_FILENAME,
    )
    return _render(request, "accounts/console/recovery_codes.html", context)


@csrf_protect
@never_cache
@require_POST
def cancel(request: HttpRequest) -> HttpResponse:
    """Leave a pending sign-in (another account, or a lost phone: ask a manager)."""
    state.clear_pending(request)
    return HttpResponseRedirect(LOGIN_PATH)


@csrf_protect
@never_cache
@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    """End the console session (Unfold's "Log out" posts here: ``admin:logout``)."""
    user = request.user
    if isinstance(user, StaffUser) and user.is_authenticated:
        events.record("console.logout", request, user)
    auth.logout(request)
    messages.info(request, LOGGED_OUT)
    return HttpResponseRedirect(LOGIN_PATH)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """``CSRF_FAILURE_VIEW``: a console page in Uzbek (403) for a console path; Django's own page elsewhere
    (no other part of the project uses session forms)."""
    if not request.path_info.startswith(CONSOLE_PREFIX):
        from django.views.csrf import csrf_failure as django_csrf_failure

        return django_csrf_failure(request, reason)
    target = LOGIN_PATH if not admin.site.has_permission(request) else state.CONSOLE_INDEX
    context = _context(request, "So‘rov qabul qilinmadi", csrf_message=CSRF_FAILED, next_url=target)
    return _render(request, "accounts/console/csrf_failure.html", context, status=403)
