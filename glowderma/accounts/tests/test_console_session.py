"""Console session limits (idle, absolute, unstamped) — ConsoleSessionMiddleware."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import SESSION_KEY
from django.test import Client, override_settings

from glowderma.accounts.console import state
from glowderma.accounts.console.middleware import TOUCH_SECONDS
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.ops_helpers import ops_device, verify_session
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db


def age(client: Client, *, started: float = 0, seen: float = 0) -> None:
    session = client.session
    now = timeutils.now().timestamp()
    session[state.STARTED] = now - started
    session[state.SEEN] = now - seen
    session.save()


def test_a_fresh_session_works_and_its_last_request_is_stamped(ops_client: Client) -> None:
    age(ops_client, started=100, seen=TOUCH_SECONDS + 1)
    assert ops_client.get("/django-admin/").status_code == 200
    assert timeutils.now().timestamp() - ops_client.session[state.SEEN] < TOUCH_SECONDS


@override_settings(OPS_SESSION_IDLE_SECONDS=1800)
def test_an_idle_session_ends_and_returns_to_the_login_page(ops_client: Client, ops_user: StaffUser) -> None:
    age(ops_client, started=1900, seen=1801)
    answer = ops_client.get("/django-admin/core/auditentry/?q=x")
    assert answer.status_code == 302
    assert answer["Location"] == "/django-admin/login/?next=%2Fdjango-admin%2Fcore%2Fauditentry%2F%3Fq%3Dx"
    assert SESSION_KEY not in ops_client.session
    entry = AuditEntry.objects.get(action="console.session_expired")
    assert entry.reason == "idle" and entry.actor_id == ops_user.pk
    page = ops_client.get(answer["Location"])
    assert "Sessiya muddati tugadi" in page.content.decode()


@override_settings(OPS_SESSION_ABSOLUTE_SECONDS=43200, OPS_SESSION_IDLE_SECONDS=1800)
def test_a_session_ends_after_its_absolute_lifetime_even_when_active(ops_client: Client) -> None:
    age(ops_client, started=43201, seen=1)
    assert ops_client.get("/django-admin/").status_code == 302
    assert AuditEntry.objects.get(action="console.session_expired").reason == "absolute"


def test_a_logged_in_session_without_the_console_stamps_is_ended(ops_user: StaffUser) -> None:
    client = Client()
    client.force_login(ops_user)
    session = client.session
    session["otp_device_id"] = ops_device(ops_user).persistent_id
    session.save()
    assert client.get("/django-admin/").status_code == 302
    assert AuditEntry.objects.get(action="console.session_expired").reason == "unstamped"


def test_a_post_after_expiry_is_not_carried_out(ops_client: Client) -> None:
    age(ops_client, started=5000, seen=5000)
    answer = ops_client.post("/django-admin/accounts/staffuser/", {"action": "reset_two_factor"})
    assert answer.status_code == 302 and answer["Location"] == "/django-admin/login/"


def test_other_paths_are_not_affected(ops_user: StaffUser) -> None:
    client = Client()
    client.force_login(ops_user)
    verify_session(client, ops_user)
    age(client, started=99999, seen=99999)
    assert client.get("/api/v1/health/").status_code == 200
    assert SESSION_KEY in client.session


def test_session_cookie_settings(settings: object) -> None:
    from django.conf import settings as django_settings

    assert django_settings.SESSION_COOKIE_SAMESITE == "Strict"
    assert django_settings.CSRF_COOKIE_SAMESITE == "Strict"
    assert django_settings.SESSION_COOKIE_DOMAIN is None
    assert django_settings.SESSION_COOKIE_HTTPONLY
    assert django_settings.SESSION_COOKIE_AGE == django_settings.OPS_SESSION_ABSOLUTE_SECONDS
    assert timedelta(seconds=django_settings.OPS_SESSION_IDLE_SECONDS) == timedelta(minutes=30)
