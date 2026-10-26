"""Writes of `site_config`: the typed settings singleton (contract §2.11)."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.core import audit, timeutils
from glowderma.site_config import selectors
from glowderma.site_config.models import SiteSettings

# Columns PATCH settings/ may change, keyed by their API names (contract §2.11).
EDITABLE_FIELDS: tuple[str, ...] = (
    "reservation_minutes",
    "support_phone",
    "pickup_address_uz",
    "pickup_address_ru",
    "pickup_address_en",
)


def settings_snapshot(row: SiteSettings) -> dict[str, Any]:
    """The audited image of the editable fields (``support_phone`` ``""`` ↔ ``null`` as in the API)."""
    return {
        "reservation_minutes": row.reservation_minutes,
        "support_phone": row.support_phone or None,
        "pickup_address": {
            "uz": row.pickup_address_uz,
            "ru": row.pickup_address_ru,
            "en": row.pickup_address_en,
        },
    }


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


@transaction.atomic
def update_settings(changes: dict[str, Any], *, actor: Any, request: Any = None) -> SiteSettings:
    """Apply ``changes`` (column name → value, ``support_phone`` may be ``None``) and audit the diff.

    Audit ``settings.updated`` (contract §2.11) carries :func:`leaf_diff` as its data: only the changed
    leaves, each ``{"from", "to"}``. A PATCH that changes nothing writes no audit entry and keeps
    ``updated_at`` / ``updated_by``.
    """
    row = SiteSettings.objects.select_for_update().get(pk=1)
    before = settings_snapshot(row)
    for name, value in changes.items():
        if name not in EDITABLE_FIELDS:
            raise ValueError(f"{name} is not an editable setting")
        setattr(row, name, "" if value is None and name == "support_phone" else value)
    diff = leaf_diff(before, settings_snapshot(row))
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
