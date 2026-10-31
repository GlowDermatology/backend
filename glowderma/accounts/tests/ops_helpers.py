"""Deterministic helpers for signing in to the ops console in tests and browser (Playwright) runs.

* ``KNOWN_TOTP_KEY`` — a fixed TOTP secret (hex), so an external test runner can compute the codes too
  (base32: ``KNOWN_TOTP_BASE32``).
* ``ops_staff(email=…, password=…)`` — an active ``is_staff`` user with a confirmed TOTP device on that key.
* ``totp_code(device)`` — the code to type now: for the current time step, or the next one when the current
  step was already used (django-otp refuses a step it has seen, and accepts the next with tolerance 1).
* ``ops_login(client, user)`` — a Django test ``Client`` logged in with a verified (2FA) console session,
  without going through the forms; ``ops_client`` in the root ``conftest.py`` wraps it.
* ``login_through_forms(client, email, password)`` — the real flow (password step, then the code step).

Browser runs: ``python manage.py shell -c "from glowderma.accounts.tests.ops_helpers import *; …"`` or import
in a pytest-playwright fixture with ``live_server``.
"""

from __future__ import annotations

import base64
import time
from binascii import unhexlify
from typing import Any

from django.test import Client
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.console import state
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import TEST_PASSWORD, StaffUserFactory
from glowderma.core import timeutils

KNOWN_TOTP_KEY = "3132333435363738393031323334353637383930"  # RFC 6238 test secret "12345678901234567890"
KNOWN_TOTP_BASE32 = base64.b32encode(unhexlify(KNOWN_TOTP_KEY)).decode()


def ops_device(user: StaffUser, key: str = KNOWN_TOTP_KEY) -> TOTPDevice:
    """The user's confirmed TOTP device on ``key`` (created if missing)."""
    device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    if device is None:
        device = TOTPDevice.objects.create(
            user=user, name="Ops konsol", key=key, confirmed=True, step=30, tolerance=1, digits=6
        )
    return device


def ops_staff(
    email: str = "ops@x.uz", password: str = TEST_PASSWORD, *, key: str = KNOWN_TOTP_KEY, **fields: Any
) -> StaffUser:
    """An active staff member with console access (``is_staff``) and a confirmed TOTP device on ``key``."""
    user = StaffUser.objects.filter(email=email).first()
    if user is None:
        user = StaffUserFactory.create(email=email, password=password, is_staff=True, **fields)
    elif not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    ops_device(user, key)
    return user


def totp_code_for_key(key: str, *, at: float | None = None, step_offset: int = 0) -> str:
    totp = TOTP(unhexlify(key), step=30, t0=0, digits=6)
    totp.time = (time.time() if at is None else at) + step_offset * 30
    return f"{totp.token():06d}"


def totp_code(device: TOTPDevice) -> str:
    """A code ``device`` accepts right now: the current step's, or the next step's if the current one (or a
    later one) was already used."""
    device.refresh_from_db()
    totp = TOTP(device.bin_key, step=device.step, t0=device.t0, digits=device.digits, drift=device.drift)
    totp.time = time.time()
    offset = 0 if totp.t() > device.last_t else device.last_t + 1 - totp.t()
    if offset > device.tolerance:
        raise RuntimeError("no fresh TOTP step within the tolerance: wait for the next 30 s step")
    totp.time += offset * device.step
    return f"{totp.token():0{device.digits}d}"


def verify_session(client: Client, user: StaffUser, device: TOTPDevice | None = None) -> None:
    """Mark the client's (logged-in) session verified by ``device`` and stamp it as a fresh console login."""
    device = device or ops_device(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    now = timeutils.now().timestamp()
    session[state.STARTED] = now
    session[state.SEEN] = now
    session.save()


def ops_login(client: Client, user: StaffUser | None = None) -> StaffUser:
    """Log ``client`` in to the console with a verified session (``user`` gets a TOTP device and
    ``is_staff`` if needed; default: ``ops_staff()``)."""
    if user is None:
        user = ops_staff()
    elif not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    device = ops_device(user)
    client.force_login(user)
    verify_session(client, user, device)
    return user


def login_through_forms(client: Client, email: str, password: str = TEST_PASSWORD) -> Any:
    """The real two-step sign-in; returns the code step's response (a redirect into the console)."""
    first = client.post("/django-admin/login/", {"username": email, "password": password})
    if first.status_code != 302 or first["Location"] != "/django-admin/2fa/":
        raise AssertionError(f"password step did not lead to the code step: {first.status_code}")
    device = TOTPDevice.objects.get(user__email=email, confirmed=True)
    return client.post("/django-admin/2fa/", {"code": totp_code(device)})
