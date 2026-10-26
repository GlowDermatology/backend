"""Writes of `site_config`: the typed settings singleton (contract §2.11).

* :func:`update_settings` — ``PATCH settings/``: the editable columns, audited as ``settings.updated``
  with the changed leaves only (the long legal texts as digests, never the texts themselves).
* :func:`publish_terms` — ``POST settings/terms/publish/``: copies the terms and privacy drafts into the
  published columns and stamps a new ``terms_version`` (the customer consent unit, customer contract §2.1.3 /
  §2.2.4), audited as ``settings.terms_published``.

The terms and the privacy policy have a draft and a published copy (GS-2): ``PATCH`` edits the drafts only,
``settings/public/`` and registration use the published copies only, so a ``terms_version`` always names one
text. The booking policy is not versioned: a saved booking policy is served at once.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from django.db import transaction

from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.site_config import selectors
from glowderma.site_config.models import SiteSettings

LANGUAGES: tuple[str, ...] = ("uz", "ru", "en")
# Contact columns stored "" and served null when empty (contract §2.11).
CONTACT_FIELDS: tuple[str, ...] = (
    "support_phone",
    "store_phone",
    "cosmetologist_phone",
    "telegram_url",
    "instagram_url",
    "website_url",
)
# Long texts: audited as digests (length + SHA-256 prefix), never as the text.
TEXT_DOCUMENTS: tuple[str, ...] = ("terms", "privacy_policy", "booking_policy")
# The two documents ``terms_version`` versions (customer contract §2.1.3).
VERSIONED_DOCUMENTS: tuple[str, ...] = ("terms", "privacy_policy")
I18N_FIELDS: tuple[str, ...] = ("pickup_address", *TEXT_DOCUMENTS)


def text_column(field: str, language: str) -> str:
    """The column ``PATCH settings/`` writes for a language of an i18n field: the draft of a versioned
    document (``terms_draft_uz``), else the served column (``booking_policy_uz``, ``pickup_address_uz``)."""
    return f"{field}_draft_{language}" if field in VERSIONED_DOCUMENTS else f"{field}_{language}"


# Columns PATCH settings/ may change, keyed by their column names (contract §2.11).
EDITABLE_FIELDS: tuple[str, ...] = (
    "reservation_minutes",
    *CONTACT_FIELDS,
    *(text_column(field, language) for field in I18N_FIELDS for language in LANGUAGES),
)
SHA256_PREFIX = 16  # hex characters of a text digest in the audit log

TERMS_INCOMPLETE = (
    "Foydalanish shartlari va maxfiylik siyosatining o‘zbekcha matnini kiriting, so‘ng e’lon qiling."
)


def settings_snapshot(row: SiteSettings) -> dict[str, Any]:
    """The image of the editable fields as the API shows them (contacts ``""`` ↔ ``null``, i18n objects; the
    terms and privacy policy are their drafts)."""
    snapshot: dict[str, Any] = {"reservation_minutes": row.reservation_minutes}
    for name in CONTACT_FIELDS:
        snapshot[name] = getattr(row, name) or None
    for field in I18N_FIELDS:
        snapshot[field] = {language: getattr(row, text_column(field, language)) for language in LANGUAGES}
    return snapshot


def unpublished_changes(row: SiteSettings) -> bool:
    """Whether a terms or privacy draft differs from its published copy (``terms_unpublished_changes``)."""
    return any(
        getattr(row, f"{document}_draft_{language}") != getattr(row, f"{document}_{language}")
        for document in VERSIONED_DOCUMENTS
        for language in LANGUAGES
    )


def leaf_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Only the changed leaves of two snapshots, each as ``{"from", "to"}``, nested like the snapshot
    (``{"pickup_address": {"ru": {"from": "", "to": "…"}}}``); ``{}`` when nothing changed."""
    diff: dict[str, Any] = {}
    for key, old in before.items():
        new = after[key]
        if isinstance(old, dict) and isinstance(new, dict):
            nested = leaf_diff(old, new)
            if nested:
                diff[key] = nested
        elif old != new:
            diff[key] = {"from": old, "to": new}
    return diff


def text_digest(text: str) -> str | None:
    """The first :data:`SHA256_PREFIX` hex characters of the text's SHA-256 (UTF-8); ``None`` for ``""``."""
    return hashlib.sha256(text.encode()).hexdigest()[:SHA256_PREFIX] if text else None


def text_change(old: str, new: str) -> dict[str, Any]:
    """The audited form of a changed long text: lengths and the new text's digest, never the text."""
    return {"changed": True, "old_len": len(old), "new_len": len(new), "sha256": text_digest(new)}


def audit_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """:func:`leaf_diff` with every changed leaf of :data:`TEXT_DOCUMENTS` replaced by :func:`text_change`."""
    diff = leaf_diff(before, after)
    for document in TEXT_DOCUMENTS:
        for language, leaf in diff.get(document, {}).items():
            diff[document][language] = text_change(leaf["from"], leaf["to"])
    return diff


@transaction.atomic
def update_settings(changes: dict[str, Any], *, actor: Any, request: Any = None) -> SiteSettings:
    """Apply ``changes`` (column name → value; a contact may be ``None``) and audit the diff.

    Audit ``settings.updated`` (contract §2.11) carries :func:`audit_diff` as its data: only the changed
    leaves, each ``{"from", "to"}`` (a long text as ``{"changed", "old_len", "new_len", "sha256"}``). A PATCH
    that changes nothing writes no audit entry and keeps ``updated_at`` / ``updated_by``. The terms and
    privacy changes land in the drafts and are published only by :func:`publish_terms`.
    """
    row = SiteSettings.objects.select_for_update().get(pk=1)
    before = settings_snapshot(row)
    for name, value in changes.items():
        if name not in EDITABLE_FIELDS:
            raise ValueError(f"{name} is not an editable setting")
        setattr(row, name, "" if value is None and name in CONTACT_FIELDS else value)
    diff = audit_diff(before, settings_snapshot(row))
    if not diff:
        return row
    row.updated_at = timeutils.now()
    row.updated_by = actor
    row.save(update_fields=[*changes, "updated_at", "updated_by"])
    # GET settings/public/ caches the singleton for 60 s (customer contract §2.4): drop it once this commits.
    transaction.on_commit(selectors.invalidate_public_settings, robust=True)
    audit.record(
        action="settings.updated",
        entity_type="settings",
        entity_id="1",
        actor=actor,
        request=request,
        after=diff,
    )
    return row


def _parse_version(version: str) -> tuple[date, int] | None:
    """``(day, n)`` of a ``YYYYMMDD-n`` version, ``None`` for any other shape (an older format, ``""``)."""
    head, separator, tail = version.partition("-")
    if not (separator and len(head) == 8 and head.isascii() and head.isdigit()):
        return None
    if not (tail.isascii() and tail.isdigit()):
        return None
    try:
        return datetime.strptime(head, "%Y%m%d").date(), int(tail)  # noqa: DTZ007 - a calendar day
    except ValueError:
        return None


def next_terms_version(current: str, today: date) -> str:
    """The version after ``current`` published on the Tashkent day ``today``: ``YYYYMMDD-n``, ``n`` counting
    the publications of that day from 1. A version is never reused: should ``current`` carry a later day
    (the clock went back), its day is kept and ``n`` grows."""
    parsed = _parse_version(current)
    if parsed is not None and parsed[0] >= today:
        day, number = parsed
        return f"{day:%Y%m%d}-{number + 1}"
    return f"{today:%Y%m%d}-1"


@transaction.atomic
def publish_terms(*, actor: Any, request: Any = None) -> SiteSettings:
    """Publish the terms and privacy drafts as a new ``terms_version`` (contract §2.11).

    Refused with 409 ``conflict`` while the uz terms or the uz privacy draft has no visible character
    (:func:`selectors.has_visible_text`: whitespace and invisible format characters do not count). Every
    draft (uz/ru/en) is copied into its published column. The new version is :func:`next_terms_version` of
    the Tashkent day; ``terms_updated_at`` = now; ``updated_at`` / ``updated_by`` follow. Every customer
    whose consent names another version is asked to accept again at checkout and booking (409
    ``terms_update_required``, customer contract §2.2.4); registration accepts only the new version.
    The public-settings cache starts a new generation on commit. Audit ``settings.terms_published``
    ``{from, to, sha256}`` (``sha256``: the digest of every published text).
    """
    row = SiteSettings.objects.select_for_update().get(pk=1)
    if not all(selectors.has_visible_text(getattr(row, f"{doc}_draft_uz")) for doc in VERSIONED_DOCUMENTS):
        raise Conflict(TERMS_INCOMPLETE)
    published_columns = [f"{doc}_{language}" for doc in VERSIONED_DOCUMENTS for language in LANGUAGES]
    for column in published_columns:
        document, language = column.rsplit("_", 1)
        setattr(row, column, getattr(row, f"{document}_draft_{language}"))
    now = timeutils.now()
    previous = row.terms_version or None
    row.terms_version = next_terms_version(row.terms_version, timeutils.local_date(now))
    row.terms_updated_at = now
    row.updated_at = now
    row.updated_by = actor
    row.save(
        update_fields=[*published_columns, "terms_version", "terms_updated_at", "updated_at", "updated_by"]
    )
    transaction.on_commit(selectors.invalidate_public_settings, robust=True)
    audit.record(
        action="settings.terms_published",
        entity_type="settings",
        entity_id="1",
        actor=actor,
        request=request,
        after={
            "from": previous,
            "to": row.terms_version,
            "sha256": {
                document: {
                    language: text_digest(getattr(row, f"{document}_{language}")) for language in LANGUAGES
                }
                for document in VERSIONED_DOCUMENTS
            },
        },
    )
    return row
