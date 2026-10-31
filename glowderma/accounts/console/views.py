"""The console's second step and its exits, Unfold-styled like the login page.

``verify`` (TOTP code), ``recovery`` (one-time recovery code) and ``enrol`` (first TOTP device: QR + manual
key, confirmed by a code, then the ten recovery codes shown once) all need a pending sign-in from the password
step and share the ``django_admin_otp`` lockout (LoginGuard: five failures a minute lock the user out for
1, 2, 4, 8, then 15 minutes). ``cancel`` drops a pending sign-in, ``logout`` ends a console session.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.contrib import admin, auth, messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST
from rest_framework.exceptions import Throttled

from glowderma.accounts.api.guard import LoginGuard, email_hash, lockout_message
from glowderma.accounts.console import events, state, twofactor
from glowderma.accounts.console.forms import RecoveryCodeForm, TotpCodeForm
from glowderma.accounts.models import StaffUser

SCOPE = "django_admin_otp"
LOGIN_PATH = "/django-admin/login/"
WRONG_CODE = "Kod noto‘g‘ri yoki allaqachon ishlatilgan. Ilovadagi yangi kodni kiriting."
WRONG_RECOVERY = "Zaxira kod noto‘g‘ri yoki allaqachon ishlatilgan."
SIGNIN_EXPIRED = "Kirish muddati tugadi. Email va parolni qaytadan kiriting."
FEW_RECOVERY_CODES = (
    "Zaxira kodlardan {left} tasi qoldi. Menejerdan 2FA ni qayta o‘rnatishni so‘rang (yangi kodlar beriladi)."
)
LOGGED_OUT = "Siz konsoldan chiqdingiz."

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
    events.record("console.otp_locked", request, user, wait=wait)
    response = _render(request, template, {**context, "lockout_message": message}, status=429)
    response["Retry-After"] = str(wait)
    return response


def _attempt(request: HttpRequest, user: StaffUser) -> Throttled | None:
    """Reserve one code attempt for ``user`` (``None``), or the running lockout."""
    try:
        LoginGuard().check(SCOPE, email_hash(user.email))
    except Throttled as exc:
        return exc
    return None


def _failed(request: HttpRequest, user: StaffUser, method: str) -> None:
    LoginGuard().record_failure(SCOPE, email_hash(user.email))
    events.record("console.otp_failed", request, user, method=method)


def _succeeded(request: HttpRequest, user: StaffUser) -> None:
    LoginGuard().reset(SCOPE, email_hash(user.email))


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
        _failed(request, user, "totp")
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
        _failed(request, user, "recovery")
        if form.is_valid():
            form.add_error(None, WRONG_RECOVERY)
    return _render(request, template, context)


@signin_step
def enrol(request: HttpRequest, current: state.Pending) -> HttpResponse:
    """First sign-in without a TOTP device: scan the QR code (or type the key), confirm with a code; the
    recovery codes are rendered in that response only and never stored in plain text."""
    user = current.user
    if twofactor.has_second_factor(user):
        return HttpResponseRedirect(reverse("ops_2fa_verify"))
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
            events.record("console.2fa_enrolled", request, user, recovery_codes=len(enrolment.recovery_codes))
            events.record("console.login_succeeded", request, user, method="enrolment")
            codes_context = _context(
                request,
                "Zaxira kodlar",
                recovery_codes=enrolment.recovery_codes,
                next_url=state.safe_next(request, current.next_url),
                email=user.email,
            )
            return _render(request, "accounts/console/recovery_codes.html", codes_context)
        _failed(request, user, "enrolment")
        if form.is_valid():
            form.add_error(None, WRONG_CODE)
    return _render(request, template, context)


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
