"""A cheap version of the review's English scanner: every console page that has rows to show (the dashboards,
each registered model's list and the change page of its first row) is rendered with the reporting tests' demo
shop, and no English UI word with an Uzbek translation may be left in what a person sees. Data (JSON payloads,
provider codes, topic names) is not UI and is left out."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from django.contrib import admin
from django.core.cache import cache
from django.test import Client
from django.urls import NoReverseMatch, reverse
from django.utils.text import capfirst

from glowderma.core import seeding
from glowderma.core.tests.test_console_i18n import ENGLISH, visible_text

pytestmark = pytest.mark.django_db

# English the review found on these pages (Django / Unfold / django_otp strings and derived field labels).
LEFTOVERS = (
    *ENGLISH,
    "Superuser status",
    "superuser status",
    "Result status",
    "result status",
    "Click to cancel",
    "This item will be deleted",
    "Created at",
    "Last used at",
    "Throttling failure",
    "Effective to",
    "Superseded by",
    "Topic",
    "October",
    "Delete",
    "Add another",
    "The groups this user belongs to",
    "Specific permissions",
    "The user that this device belongs to",
    "Is this device ready for use",
)
JSON_BLOCK = re.compile(rb'<pre class="gd-json">.*?</pre>', re.S)


@pytest.fixture(autouse=True)
def _demo_shop() -> Iterator[None]:
    from glowderma.reporting.tests.test_ops_dashboards import NOW, populate

    cache.clear()
    with seeding.pinned_clock(NOW):
        populate()
        yield


def _text(content: bytes) -> str:
    return visible_text(JSON_BLOCK.sub(b"", content))


def _leftovers(text: str, model: type | None = None) -> list[str]:
    found = [word for word in LEFTOVERS if re.search(rf"(?<![\w‘’]){re.escape(word)}(?![\w‘’])", text)]
    if model is not None:
        lines = {line.strip() for line in text.split("\n")}
        for field in model._meta.concrete_fields:  # type: ignore[attr-defined]
            derived = capfirst(field.name.replace("_", " "))
            if "_" in field.name and derived in lines:
                found.append(derived)
    return found


def _pages() -> list[tuple[str, type | None]]:
    pages: list[tuple[str, type | None]] = [
        (reverse("admin:index"), None),
        (reverse("admin:reporting_money"), None),
        (reverse("admin:reporting_sales"), None),
        (reverse("admin:reporting_customers"), None),
    ]
    for model in admin.site._registry:
        opts = model._meta
        try:
            pages.append((reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist"), model))
        except NoReverseMatch:
            continue
        first = model._default_manager.order_by("pk").first()
        if first is not None:
            pages.append(
                (reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[first.pk]), model)
            )
    return pages


def test_console_pages_leave_no_english_ui_words(ops_client: Client) -> None:
    problems = {}
    pages = _pages()
    assert len(pages) > 80  # every list, and the change page of each model the demo shop fills
    for url, model in pages:
        response = ops_client.get(url)
        assert response.status_code == 200, url
        found = _leftovers(_text(response.content), model)
        if found:
            problems[url] = found
    assert problems == {}
