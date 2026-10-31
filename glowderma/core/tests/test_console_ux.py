"""The console UX review fixes shared by every read-only page (glowderma.core.admin): the applied-filters
bar, the hidden history button, so‘m amounts on change pages and Uzbek pagination."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.urls import reverse

from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.templatetags.ops_admin import applied_filters
from glowderma.core.tests.console import console_client
from glowderma.core.tests.test_console_i18n import visible_text

pytestmark = pytest.mark.django_db


def _message(key: str, **fields: object) -> OutboxMessage:
    return OutboxMessage.objects.create(topic="sms.send", payload={}, dedupe_key=key, **fields)


def test_applied_filters_name_every_lookup_in_uzbek() -> None:
    rows = applied_filters(
        OutboxMessage,
        {
            "status__in": "pending,failed",
            "available_at__lt": "2026-10-31T20:17:00+05:00",
            "deferred_since__isnull": "True",
            "topic__exact": "sms.send",
            "q": "ignored",
            "o": "1",
            "updated_at_from_0": "x",  # a range filter's own parameter: the filter panel shows it
        },
    )
    assert rows == [
        {"label": "Holat", "op": ":", "value": "Navbatda, Xato · qayta uriniladi"},
        {"label": "Navbatdagi urinish", "op": "<", "value": "31.10.2026 20:17"},
        {"label": "Kechiktirilgan", "op": "", "value": "bo‘sh"},
        {"label": "Mavzu", "op": ":", "value": "sms.send"},
    ]


def test_applied_filters_shorten_long_id_lists() -> None:
    ids = ",".join(str(n) for n in range(1, 101))
    (row,) = applied_filters(OutboxMessage, {"id__in": ids})
    assert row["value"] == "1, 2, 3, 4, 5 … (jami 100 ta)"


def test_a_drill_down_shows_what_it_applies_and_how_to_clear_it() -> None:
    now = timeutils.now()
    _message("late", status="pending", available_at=now - timedelta(hours=2))
    _message("fresh", status="pending", available_at=now + timedelta(hours=2))
    url = reverse("admin:core_outboxmessage_changelist")
    page = console_client("staff").get(url, {"status__in": "pending", "available_at__lt": now.isoformat()})
    text = visible_text(page.content)
    assert page.context["cl"].result_count == 1
    assert "Qo‘llangan filtrlar:" in text and "Navbatdagi urinish" in text and "Filtrlarni tozalash" in text
    plain = console_client("staff").get(url)
    assert "Qo‘llangan filtrlar:" not in visible_text(plain.content)


# --- labels: no Django-derived English ----------------------------------------------------------------------


def test_every_field_of_a_console_model_has_an_explicit_uzbek_label() -> None:
    """``Field.verbose_name`` not given means Django shows the English name («Last attempt at»). django_otp's
    fields get theirs at start-up (glowderma.core.otp_apps): checked by the label, not the declaration."""
    from django.contrib import admin

    missing = []
    for model in admin.site._registry:
        for field in model._meta.concrete_fields:
            if field.name == "id":
                continue
            derived = field.name.replace("_", " ")
            declared = getattr(field, "_verbose_name", None)
            if model.__module__.startswith("glowderma.") and declared is None:
                missing.append(f"{model._meta.label}.{field.name}")
            elif str(field.verbose_name) == derived and "_" in field.name:
                missing.append(f"{model._meta.label}.{field.name}")
    assert missing == []


def test_list_columns_and_readonly_extras_carry_an_uzbek_header() -> None:
    from django.contrib import admin

    for model, model_admin in admin.site._registry.items():
        field_names = {field.name for field in model._meta.get_fields()}
        for entry in (*model_admin.list_display, *getattr(model_admin, "readonly_fields", ())):
            if isinstance(entry, str) and (entry in field_names or not hasattr(model_admin, entry)):
                continue  # a model field: labelled above
            column = getattr(model_admin, entry) if isinstance(entry, str) else entry
            description = getattr(column, "short_description", None)
            assert description, (model._meta.label, entry)
            assert not re.search(r"[og]'", str(description)), description


# --- the read-only page chrome ------------------------------------------------------------------------------


def test_read_only_change_pages_have_no_history_button() -> None:
    message = _message("history", status="dead")
    page = console_client("staff").get(reverse("admin:core_outboxmessage_change", args=[message.pk]))
    assert page.context["show_history"] is False
    assert reverse("admin:core_outboxmessage_history", args=[message.pk]) not in page.content.decode()


def test_money_fields_read_as_som_on_a_change_page() -> None:
    from glowderma.payments.tests.factories import PaymentFactory

    payment = PaymentFactory.create(amount=185_000)
    page = console_client("staff").get(reverse("admin:payments_payment_change", args=[payment.pk]))
    text = visible_text(page.content)
    assert "185\xa0000 so‘m" in text
    assert "\n185000\n" not in f"\n{text}\n"


def test_pagination_speaks_uzbek_numerals() -> None:
    _message("one", status="done")
    _message("two", status="done")
    page = console_client("staff").get(reverse("admin:core_outboxmessage_changelist"))
    text = " ".join(visible_text(page.content).split())
    assert "jami 2 ta outbox xabari" in text and "2 outbox xabarlari" not in text


def test_outbox_list_has_a_short_row_action_and_no_dedupe_column() -> None:
    _message("dead-one", status="dead")
    page = console_client("staff").get(reverse("admin:core_outboxmessage_changelist"))
    html = page.content.decode()
    assert 'aria-label="Qayta navbatga qo‘yish"' in html and ">Qayta</a>" in html
    assert "Takrorlanmaslik kaliti" not in visible_text(page.content)


def test_no_breadcrumb_repeats_the_app_name_as_the_model_name() -> None:
    """«To‘lovlar › To‘lovlar»: an app is never named like one of its model lists."""
    from django.apps import apps
    from django.contrib import admin

    for model in admin.site._registry:
        app_name = str(apps.get_app_config(model._meta.app_label).verbose_name).lower()
        assert app_name != str(model._meta.verbose_name_plural).lower(), model._meta.label


def test_a_requeue_clears_the_sidebar_badge_and_the_dashboards(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    from django.core.cache import cache

    from glowderma.core.admin import DASHBOARD_CACHE_PREFIX, NAV_CACHE_PREFIX
    from glowderma.core.tests.console import confirm
    from glowderma.reporting import admin_nav, dashboards

    assert admin_nav.CACHE_PREFIX == NAV_CACHE_PREFIX and dashboards.CACHE_PREFIX == DASHBOARD_CACHE_PREFIX
    dead = _message("badge", status="dead")
    cache.set("ops:nav:dead_outbox", 1)
    cache.set("ops:dash:ops", {"stale": True})
    client = console_client("staff")
    with django_capture_on_commit_callbacks(execute=True):
        confirm(client, reverse("admin:core_outboxmessage_requeue", args=[dead.pk]))
    assert cache.get("ops:nav:dead_outbox") is None and cache.get("ops:dash:ops") is None


def test_the_confirm_step_colours_only_real_warnings() -> None:
    from glowderma.core import outbox

    dead = _message("warn", status="dead")
    dead.topic = "warn.topic"
    dead.save(update_fields=["topic"])
    hooks = outbox.RequeueHooks(
        describe=lambda message: "",
        skip_reason=lambda message: "",
        warning="w",
        warning_uz="avval tekshiring",
    )
    outbox.on_requeue("warn.topic", hooks)
    try:
        page = console_client("staff").get(reverse("admin:core_outboxmessage_requeue", args=[dead.pk]))
    finally:
        outbox._requeue_hooks.pop("warn.topic", None)
    notes = page.context["action_notes"]
    assert [note["warning"] for note in notes] == [False, True]
    assert notes[0]["text"].startswith("Mavzu: warn.topic")
    html = page.content.decode()
    assert html.count("gd-ops-note--warning") == 1


def test_the_sign_in_page_has_the_brand_side_image() -> None:
    from django.test import Client

    page = Client().get(reverse("admin:login"))
    assert "ops/login-side" in page.content.decode()
