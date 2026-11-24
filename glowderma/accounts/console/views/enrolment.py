"""The console's first-time enrolment (inside a manager-opened window) and the one page that shows its
recovery codes."""

from __future__ import annotations

import logging
from urllib.parse import quote

from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.generic import View

from glowderma.accounts import console_sessions, second_factor
from glowderma.accounts.console import access, events, notices, paths
from glowderma.accounts.console.forms import TotpCodeForm
from glowderma.accounts.console.views import code_steps, rendering
from glowderma.accounts.models import StaffUser
from glowderma.core import alerts

logger = logging.getLogger("glowderma.accounts.console.views")


CODES_FILENAME = "glowderma-ops-zaxira-kodlar.txt"  # a file name: the same in every language


def _refuse_enrolment(request: HttpRequest, user: StaffUser, reason: str) -> HttpResponse:
    """No enrolment without an open window (module doc): the pending sign-in ends, the login page says why."""
    second_factor.CONSOLE.totp_devices(user).filter(confirmed=False).delete()
    console_sessions.clear_pending(request)
    events.record("console.enrol_refused", request, user, reason=reason)
    messages.error(
        request, notices.ENROL_SAME_MANAGER if reason == access.SAME_MANAGER else notices.ENROL_CLOSED
    )
    return HttpResponseRedirect(paths.LOGIN_PATH)


class EnrolmentView(code_steps.SignInStepView):
    """First sign-in without a TOTP device, inside an open enrolment window: scan the QR code (or type the
    key), confirm with a code; the recovery codes follow on ``recovery_codes`` (shown once, never stored in
    plain text beyond that one redirect)."""

    def step(self, request: HttpRequest, current: console_sessions.Pending) -> HttpResponse:
        user = current.user
        if second_factor.has_second_factor(user, scope=second_factor.CONSOLE):
            return HttpResponseRedirect(reverse("ops_2fa_verify"))
        window = access.enrolment_window(user)
        refusal = access.enrolment_refusal(user, window)
        if refusal or window is None:
            return _refuse_enrolment(request, user, refusal or access.NO_WINDOW)
        device = second_factor.enrolment_device(
            user, request.session.get(console_sessions.ENROLMENT_DEVICE), scope=second_factor.CONSOLE
        )
        request.session[console_sessions.ENROLMENT_DEVICE] = device.pk
        form = TotpCodeForm(request.POST if request.method == "POST" else None)
        template = "accounts/console/enrol.html"
        context = rendering.console_context(
            request,
            gettext("Ikki bosqichli himoyani yoqish"),
            form=form,
            email=user.email,
            qr_svg=second_factor.qr_svg(device),
            manual_key=second_factor.manual_key(device),
        )
        if request.method == "POST":
            if (locked := code_steps.reserve_attempt(request, user)) is not None:
                return code_steps.locked_response(request, user, locked, template, context)
            enrolment = (
                second_factor.confirm_enrolment(
                    user, device.pk, form.cleaned_data["code"], scope=second_factor.CONSOLE
                )
                if form.is_valid()
                else None
            )
            if enrolment is not None:
                code_steps.record_success(request, user)
                console_sessions.complete_login(request, user, enrolment.device)
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
                logger.warning(
                    "console.2fa_enrolled staff_id=%s window_audit_id=%s", user.pk, window.audit_id
                )
                alerts.alert(
                    "console.2fa_enrolled",
                    {"staff_id": user.pk, "window_audit_id": window.audit_id, "via": window.via},
                    scope=str(user.pk),
                )
                console_sessions.keep_recovery_codes(
                    request, enrolment.recovery_codes, console_sessions.safe_next(request, current.next_url)
                )
                return HttpResponseRedirect(reverse("ops_2fa_recovery_codes"))
            code_steps.record_failed_attempt(request, user, "enrolment", form)
            if form.is_valid():
                form.add_error(None, notices.WRONG_CODE)
        return rendering.render_step(request, template, context)


@method_decorator(never_cache, name="dispatch")
class RecoveryCodesView(View):
    """The recovery codes of the enrolment that just finished, exactly once (they leave the session on this
    request); afterwards the page only says they were shown once. A verified console session only."""

    http_method_names = ["get"]

    def get(self, request: HttpRequest) -> HttpResponse:
        request.current_app = admin.site.name
        if not admin.site.has_permission(request):
            return HttpResponseRedirect(paths.LOGIN_PATH)
        kept = console_sessions.take_recovery_codes(request)
        if kept is None:
            context = rendering.console_context(
                request,
                gettext("Zaxira kodlar"),
                shown_once=notices.CODES_SHOWN_ONCE,
                next_url=console_sessions.CONSOLE_INDEX,
            )
            return rendering.render_step(request, "accounts/console/recovery_codes.html", context)
        codes, next_url = kept
        # The first line of the saved codes (the .txt download and what "copy" puts on the clipboard).
        header = gettext("Glow Derma Ops — zaxira kodlar")
        text = header + "\n" + "\n".join(codes) + "\n"
        context = rendering.console_context(
            request,
            gettext("Zaxira kodlar"),
            recovery_codes=codes,
            codes_header=header,
            next_url=next_url,
            email=getattr(request.user, "email", ""),
            codes_download=f"data:text/plain;charset=utf-8,{quote(text)}",
            codes_filename=CODES_FILENAME,
        )
        return rendering.render_step(request, "accounts/console/recovery_codes.html", context)


__all__ = [
    "CODES_FILENAME",
    "EnrolmentView",
    "RecoveryCodesView",
]
