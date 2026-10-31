"""Session state of the console sign-in.

A correct password does not log anyone in: it records a *pending* sign-in (user id, the user's session auth
hash, when, where to go next) in a fresh session key. Only the second step — a TOTP code, a recovery code or
a confirmed enrolment — calls ``django.contrib.auth.login`` (which cycles the session key again) and marks the
session verified (``django_otp.login``). A pending sign-in expires after ``PENDING_SECONDS`` and dies with a
password change (the auth hash no longer matches).

A console session carries its login instant (``STARTED``) and the instant of its last request (``SEEN``):
``ConsoleSessionMiddleware`` ends it after ``OPS_SESSION_IDLE_SECONDS`` without a request or
``OPS_SESSION_ABSOLUTE_SECONDS`` after the login.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.contrib import auth
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.http import HttpRequest
from django.utils.http import url_has_allowed_host_and_scheme
from django_otp import login as otp_login
from django_otp.models import Device

from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils

PENDING = "ops_signin"
ENROLMENT_DEVICE = "ops_enrolment_device"
# The fresh recovery codes between the enrolment POST and the page that shows them (one redirect).
RECOVERY_CODES = "ops_recovery_codes"
STARTED = "ops_started_at"
SEEN = "ops_seen_at"
PENDING_SECONDS = 300
CONSOLE_INDEX = "/django-admin/"
BACKEND = "django.contrib.auth.backends.ModelBackend"


def _now() -> float:
    return timeutils.now().timestamp()


def safe_next(request: HttpRequest, candidate: str | None) -> str:
    """``candidate`` when it is a path on this host, else the console index."""
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return CONSOLE_INDEX


def start_pending(request: HttpRequest, user: StaffUser, next_url: str) -> None:
    """The password step passed: a new session key (no fixation) holding only the pending sign-in."""
    request.session.flush()
    request.session[PENDING] = {
        "uid": user.pk,
        "hash": user.get_session_auth_hash(),
        "at": _now(),
        "next": next_url,
    }


@dataclass(frozen=True)
class Pending:
    user: StaffUser
    next_url: str


def pending(request: HttpRequest) -> Pending | None:
    """The sign-in in progress of this session, if it is still valid (fresh, the user still an active staff
    member with the same password); a stale one is dropped."""
    data: Any = request.session.get(PENDING)
    if not isinstance(data, dict) or not isinstance(data.get("uid"), int):
        return None
    user = StaffUser.objects.filter(pk=int(data["uid"]), is_active=True, is_staff=True).first()
    fresh = _now() - float(data.get("at", 0)) <= PENDING_SECONDS
    if user is None or not fresh or data.get("hash") != user.get_session_auth_hash():
        clear_pending(request)
        return None
    return Pending(user, str(data.get("next") or CONSOLE_INDEX))


def clear_pending(request: HttpRequest) -> None:
    request.session.pop(PENDING, None)
    request.session.pop(ENROLMENT_DEVICE, None)


def stamp(request: HttpRequest) -> None:
    now = _now()
    request.session[STARTED] = now
    request.session[SEEN] = now


def complete_login(request: HttpRequest, user: StaffUser, device: Device | None) -> None:
    """Log ``user`` in (a new session key) and mark the session verified by ``device``."""
    clear_pending(request)
    auth.login(request, user, backend=BACKEND)
    if device is not None:
        otp_login(request, device)
    stamp(request)


def keep_recovery_codes(request: HttpRequest, codes: list[str], next_url: str) -> None:
    """Hold the enrolment's recovery codes for the one GET that shows them (``take_recovery_codes``)."""
    request.session[RECOVERY_CODES] = {"codes": list(codes), "next": next_url, "at": _now()}


def take_recovery_codes(request: HttpRequest) -> tuple[list[str], str] | None:
    """The held recovery codes and the destination after them, removed from the session (shown once); ``None``
    when there are none or they were held longer than ``PENDING_SECONDS``."""
    data: Any = request.session.pop(RECOVERY_CODES, None)
    if not isinstance(data, dict) or _now() - float(data.get("at", 0)) > PENDING_SECONDS:
        return None
    codes = [str(code) for code in data.get("codes") or []]
    return (codes, str(data.get("next") or CONSOLE_INDEX)) if codes else None


def end_console_sessions(user: StaffUser) -> int:
    """Delete every unexpired session of ``user`` — logged in, or with a sign-in pending (database
    sessions: decoded one by one; the console is the only session user of the project)."""
    ended = 0
    for session in Session.objects.filter(expire_date__gt=timeutils.now()).iterator():
        data = session.get_decoded()
        signin = data.get(PENDING)
        if str(data.get(SESSION_KEY)) == str(user.pk) or (
            isinstance(signin, dict) and signin.get("uid") == user.pk
        ):
            session.delete()
            ended += 1
    return ended
