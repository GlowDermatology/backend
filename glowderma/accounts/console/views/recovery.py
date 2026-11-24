"""The console's recovery-code step: one of the user's one-time codes instead of the TOTP code."""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext

from glowderma.accounts import console_sessions, second_factor
from glowderma.accounts.console import events, notices
from glowderma.accounts.console.forms import RecoveryCodeForm
from glowderma.accounts.console.views import code_steps, rendering


class RecoveryCodeSignInView(code_steps.SignInStepView):
    """One of the user's recovery codes instead of the TOTP code (the code is used up)."""

    def step(self, request: HttpRequest, current: console_sessions.Pending) -> HttpResponse:
        user = current.user
        if not second_factor.has_second_factor(user, scope=second_factor.CONSOLE):
            return HttpResponseRedirect(reverse("ops_2fa_enrol"))
        form = RecoveryCodeForm(request.POST if request.method == "POST" else None)
        template = "accounts/console/recovery.html"
        context = rendering.console_context(
            request, gettext("Zaxira kod bilan kirish"), form=form, email=user.email
        )
        if request.method == "POST":
            if (locked := code_steps.reserve_attempt(request, user)) is not None:
                return code_steps.locked_response(request, user, locked, template, context)
            used = (
                second_factor.use_recovery_code(user, form.cleaned_data["code"], scope=second_factor.CONSOLE)
                if form.is_valid()
                else None
            )
            if used is not None:
                code_steps.record_success(request, user)
                console_sessions.complete_login(request, user, used.device)
                events.record("console.recovery_code_used", request, user, remaining=used.remaining)
                events.record("console.login_succeeded", request, user, method="recovery")
                if used.remaining <= 3:
                    messages.warning(request, notices.few_recovery_codes(used.remaining))
                return HttpResponseRedirect(console_sessions.safe_next(request, current.next_url))
            code_steps.record_failed_attempt(request, user, "recovery", form)
            if form.is_valid():
                form.add_error(None, notices.WRONG_RECOVERY)
        return rendering.render_step(request, template, context)


__all__ = [
    "RecoveryCodeSignInView",
]
