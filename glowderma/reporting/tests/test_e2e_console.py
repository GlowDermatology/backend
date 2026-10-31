"""``manage.py e2e_console``: refused outside the e2e settings; its database actions stay on e2e databases."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from glowderma.accounts.models import StaffUser
from glowderma.reporting.management.commands import e2e_console
from glowderma.reporting.management.commands.e2e_console import MARKER, Command


def run(action: str, data: dict[str, Any] | None = None) -> Any:
    out = StringIO()
    call_command(Command(), action, "--data", json.dumps(data or {}), stdout=out)
    line = out.getvalue().strip().splitlines()[-1]
    assert line.startswith(MARKER)
    return json.loads(line[len(MARKER) :])


def test_refused_under_the_test_and_local_settings() -> None:
    with pytest.raises(CommandError, match="needs the e2e settings"):
        run("registry")


@override_settings(E2E_CONSOLE_ENABLED=True, DEV_FEATURES_ALLOWED=False)
def test_refused_without_the_development_switches() -> None:
    with pytest.raises(CommandError, match="DEV_FEATURES_ALLOWED"):
        run("registry")


@override_settings(E2E_CONSOLE_ENABLED=True, SETTINGS_MODULE="config.settings.production")
def test_refused_under_the_production_settings_module_whatever_the_flag() -> None:
    with pytest.raises(CommandError, match="production"):
        run("registry")


@override_settings(E2E_CONSOLE_ENABLED="yes")
def test_only_a_real_true_switches_it_on() -> None:
    with pytest.raises(CommandError, match="needs the e2e settings"):
        run("registry")


def test_the_production_settings_force_the_switch_off() -> None:
    """Never read from the environment: only config.settings.e2e turns it on, production pins it off."""
    production = (settings.BASE_DIR / "config" / "settings" / "production.py").read_text(encoding="utf-8")
    assert "\nE2E_CONSOLE_ENABLED = False\n" in production
    assert settings.E2E_CONSOLE_ENABLED is False  # the test settings (local) leave the base default


@override_settings(E2E_CONSOLE_ENABLED=True)
def test_the_registry_lists_every_console_model() -> None:
    rows = run("registry")
    labels = {row["label"] for row in rows}
    assert {"core.outboxmessage", "professionals.professional", "otp_totp.totpdevice"} <= labels
    assert "auth.group" not in labels
    outbox = next(row for row in rows if row["label"] == "core.outboxmessage")
    assert outbox["read_only"] is True
    assert outbox["changelist"] == "/django-admin/core/outboxmessage/"


@override_settings(E2E_CONSOLE_ENABLED=True)
@pytest.mark.parametrize("name", ["glowderma", "glowderma_unfold", "test_glowderma", "glowderma_e2e; DROP"])
def test_reset_db_refuses_any_other_database(name: str) -> None:
    with pytest.raises(CommandError, match="refusing"):
        run("reset-db", {"names": [name]})


@override_settings(E2E_CONSOLE_ENABLED=True)
def test_reset_db_refuses_another_template() -> None:
    with pytest.raises(CommandError, match="refusing template"):
        run("reset-db", {"names": ["glowderma_e2e_x"], "template": "glowderma"})


@pytest.mark.django_db
@override_settings(E2E_CONSOLE_ENABLED=True)
def test_staff_and_session_actions() -> None:
    created = run("staff", {"manager": True, "recovery": True})
    user = StaffUser.objects.get(pk=created["id"])
    assert user.is_staff and user.is_manager and user.check_password(created["password"])
    assert len(created["key_hex"]) == 40 and len(created["recovery_codes"]) == 10
    session = run("session", {"email": user.email})
    assert run("session-state", {"session_key": session["session_key"]}) == {
        "exists": True,
        "user_id": str(user.pk),
        "verified": True,
        "pending": False,
    }
    unverified = run("session", {"email": user.email, "verified": False})
    assert run("session-state", {"session_key": unverified["session_key"]})["verified"] is False


def test_secret_fields_cover_the_excluded_columns() -> None:
    from django.contrib import admin

    from glowderma.accounts.models import AuthSession, Device

    assert e2e_console._secret_fields(AuthSession, admin.site._registry[AuthSession]) == [
        "previous_refresh_hash",
        "refresh_hash",
    ]
    assert e2e_console._secret_fields(Device, admin.site._registry[Device]) == ["fcm_token", "key_hash"]


def test_a_column_excluded_only_for_its_labelled_display_is_no_secret() -> None:
    from django.contrib import admin

    from glowderma.delivery.models import Shipment

    # bts_status is shown as «code — Uzbek label» (bts_status_label), not hidden
    assert "bts_status" not in e2e_console._secret_fields(Shipment, admin.site._registry[Shipment])


@pytest.mark.django_db
@override_settings(E2E_CONSOLE_ENABLED=True)
def test_enrolment_window_and_password_reset_actions() -> None:
    from glowderma.accounts.console import access
    from glowderma.core.models import AuditEntry

    without = run("staff", {"totp": False})
    assert "enrol_until" not in without
    assert access.enrolment_window(StaffUser.objects.get(pk=without["id"])) is None
    opened = run("staff", {"totp": False, "enrol_window": True})
    assert opened["enrol_until"]
    window = access.enrolment_window(StaffUser.objects.get(pk=opened["id"]))
    assert window is not None and window.via == access.VIA_CLI
    manager = run("staff", {"manager": True})
    assert run("open-enrolment", {"email": without["email"], "actor": manager["email"]})["until"]
    window = access.enrolment_window(StaffUser.objects.get(pk=without["id"]))
    assert window is not None and window.via == access.VIA_CONSOLE and window.opened_by_id == manager["id"]
    reset = run("reset-password", {"email": without["email"], "actor": manager["email"]})
    assert StaffUser.objects.get(pk=without["id"]).check_password(reset["password"])
    entry = AuditEntry.objects.get(action="staff.password_reset")
    rows = run("audit", {"action": "staff.password_reset"})
    assert rows[-1]["id"] == entry.pk and "ip" in rows[-1]


@pytest.mark.django_db
@override_settings(E2E_CONSOLE_ENABLED=True)
def test_outbox_secrets_and_english_labels_actions() -> None:
    from glowderma.core.models import OutboxMessage

    created = run("outbox-secrets")
    topics = sorted(message["topic"] for message in created["messages"])
    assert topics == ["apple.revoke", "push.account_security"]
    assert set(OutboxMessage.objects.values_list("status", flat=True)) == {"dead"}
    payloads = " ".join(str(row.payload) for row in OutboxMessage.objects.all())
    assert all(secret in payloads for secret in created["secrets"])  # stored as produced, masked on the page
    labels = run("english-labels")
    assert "delivery.shipment" in labels and "commerce.order" in labels
    # every field of every console model (and of its inlines) has an explicit Uzbek label
    assert {label: found for label, found in labels.items() if found} == {}


def test_derived_labels_find_a_field_without_a_label_and_ignore_relabelled_ones() -> None:
    from types import SimpleNamespace

    from django.db import models

    bare = models.DateTimeField()
    bare.set_attributes_from_name("last_attempt_at")
    named = models.CharField(verbose_name="email")
    named.set_attributes_from_name("email")
    relabelled = models.BooleanField()  # a third-party field given an Uzbek label at start-up (otp_apps)
    relabelled.set_attributes_from_name("confirmed")
    relabelled.verbose_name = "tasdiqlangan"
    fake = SimpleNamespace(_meta=SimpleNamespace(get_fields=lambda: [bare, named, relabelled]))
    assert e2e_console._derived_labels(fake) == ["Last attempt at"]  # type: ignore[arg-type]


def test_filters_place_a_filter_shown_only_when_applied() -> None:
    from django.contrib import admin

    from glowderma.core.models import OutboxMessage

    filters = e2e_console._filters(OutboxMessage, admin.site._registry[OutboxMessage])
    by_field = {entry["field"]: entry for entry in filters}
    assert by_field["order"]["shown"] is False  # the ?order= drill-down of an order's Diagnostika block
    deferred = by_field["deferred_since__isnull"]
    assert (deferred["shown"], deferred["index"], deferred["block"]) == (True, 3, 2)
