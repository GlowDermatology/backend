"""Leaving the console: cancelling a pending sign-in and logging out."""

from __future__ import annotations

from django.contrib import auth, messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from glowderma.accounts import console_sessions
from glowderma.accounts.console import events, notices, paths
from glowderma.accounts.models import StaffUser


@csrf_protect
@never_cache
@require_POST
def cancel(request: HttpRequest) -> HttpResponse:
    """Leave a pending sign-in (another account, or a lost phone: ask a manager)."""
    console_sessions.clear_pending(request)
    return HttpResponseRedirect(paths.LOGIN_PATH)


@csrf_protect
@never_cache
@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    """End the console session (Unfold's "Log out" posts here: ``admin:logout``)."""
    user = request.user
    if isinstance(user, StaffUser) and user.is_authenticated:
        events.record("console.logout", request, user)
    auth.logout(request)
    messages.info(request, notices.LOGGED_OUT)
    return HttpResponseRedirect(paths.LOGIN_PATH)


__all__ = [
    "cancel",
    "logout",
]
