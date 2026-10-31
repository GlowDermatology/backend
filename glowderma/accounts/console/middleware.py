"""Idle and absolute limits of a console session (``OPS_SESSION_IDLE_SECONDS`` / ``_ABSOLUTE_SECONDS``).

Only ``/django-admin`` requests of a logged-in session are looked at (sessions exist for the console alone).
A session without the login stamp (logged in some other way) is ended as well: every console login goes
through ``state.complete_login``. The last-request stamp is written at most every ``TOUCH_SECONDS``.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import auth, messages
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.base import SessionBase
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

from glowderma.accounts.console import events, state
from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils
from glowderma.core.network import CONSOLE_PREFIX

TOUCH_SECONDS = 15
LOGIN_PATH = "/django-admin/login/"
EXPIRED_MESSAGE = "Sessiya muddati tugadi. Qaytadan kiring."


def expiry_reason(session: SessionBase, now: float) -> str | None:
    started = session.get(state.STARTED)
    seen = session.get(state.SEEN)
    if not isinstance(started, (int, float)) or not isinstance(seen, (int, float)):
        return "unstamped"
    if now - seen > settings.OPS_SESSION_IDLE_SECONDS:
        return "idle"
    if now - started > settings.OPS_SESSION_ABSOLUTE_SECONDS:
        return "absolute"
    return None


class ConsoleSessionMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path_info.startswith(CONSOLE_PREFIX) and SESSION_KEY in request.session:
            now = timeutils.now().timestamp()
            reason = expiry_reason(request.session, now)
            if reason is not None:
                user = StaffUser.objects.filter(pk=str(request.session[SESSION_KEY])).first()
                events.record("console.session_expired", request, user, reason=reason)
                auth.logout(request)
                messages.warning(request, EXPIRED_MESSAGE)
                target = LOGIN_PATH
                if request.method == "GET" and request.path_info != LOGIN_PATH:
                    target += "?" + urlencode({"next": request.get_full_path()})
                return HttpResponseRedirect(target)
            if now - float(request.session[state.SEEN]) >= TOUCH_SECONDS:
                request.session[state.SEEN] = now
        return self.get_response(request)
