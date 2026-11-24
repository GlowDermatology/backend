"""Leaving the console: cancelling a pending sign-in and logging out."""

from __future__ import annotations

from django.contrib import auth, messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.generic import View

from glowderma.accounts import console_sessions
from glowderma.accounts.console import events, notices, paths
from glowderma.accounts.models import StaffUser


@method_decorator([csrf_protect, never_cache], name="dispatch")
class CancelSignInView(View):
    """Leave a pending sign-in (another account, or a lost phone: ask a manager)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        console_sessions.clear_pending(request)
        return HttpResponseRedirect(paths.LOGIN_PATH)


@method_decorator([csrf_protect, never_cache], name="dispatch")
class ConsoleLogoutView(View):
    """End the console session (Unfold's "Log out" posts here: ``admin:logout``)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        if isinstance(user, StaffUser) and user.is_authenticated:
            events.record("console.logout", request, user)
        auth.logout(request)
        messages.info(request, notices.LOGGED_OUT)
        return HttpResponseRedirect(paths.LOGIN_PATH)


__all__ = [
    "CancelSignInView",
    "ConsoleLogoutView",
]
