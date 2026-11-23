"""The console's TOTP step: the code of the user's confirmed device."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext

from glowderma.accounts import console_sessions, second_factor
from glowderma.accounts.console import events, notices
from glowderma.accounts.console.forms import TotpCodeForm
from glowderma.accounts.console.views import code_steps, rendering


@code_steps.signin_step
def verify(request: HttpRequest, current: console_sessions.Pending) -> HttpResponse:
    """The TOTP code of the user's confirmed device."""
    user = current.user
    if not second_factor.has_second_factor(user, scope=second_factor.CONSOLE):
        return HttpResponseRedirect(reverse("ops_2fa_enrol"))
    form = TotpCodeForm(request.POST if request.method == "POST" else None)
    template = "accounts/console/verify.html"
    context = rendering.console_context(
        request, gettext("Ikki bosqichli tekshiruv"), form=form, email=user.email
    )
    if request.method == "POST":
        if (locked := code_steps.reserve_attempt(request, user)) is not None:
            return code_steps.locked_response(request, user, locked, template, context)
        device = (
            second_factor.verify_totp(user, form.cleaned_data["code"], scope=second_factor.CONSOLE)
            if form.is_valid()
            else None
        )
        if device is not None:
            code_steps.record_success(request, user)
            console_sessions.complete_login(request, user, device)
            events.record("console.login_succeeded", request, user, method="totp")
            return HttpResponseRedirect(console_sessions.safe_next(request, current.next_url))
        code_steps.record_failed_attempt(request, user, "totp", form)
        if form.is_valid():
            form.add_error(None, notices.WRONG_CODE)
    return rendering.render_step(request, template, context)


__all__ = [
    "verify",
]
