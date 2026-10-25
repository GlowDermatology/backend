"""Writes of `site_config`: the typed settings singleton (contract §2.11)."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.core import audit, timeutils
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


@transaction.atomic
def update_settings(changes: dict[str, Any], *, actor: Any, request: Any = None) -> SiteSettings:
    """Apply ``changes`` (column name → value, ``support_phone`` may be ``None``) and audit the diff.

    Audit ``settings.updated`` carries ``before``/``after`` restricted to the keys that actually changed;
    an idempotent PATCH (nothing changed) writes no audit entry and keeps ``updated_at``.
    """
    row = SiteSettings.objects.select_for_update().get(pk=1)
    before = settings_snapshot(row)
    for name, value in changes.items():
        if name not in EDITABLE_FIELDS:
            raise ValueError(f"{name} is not an editable setting")
        setattr(row, name, "" if value is None and name == "support_phone" else value)
    after = settings_snapshot(row)
    diff_before = {key: value for key, value in before.items() if after[key] != value}
    if not diff_before:
        return row
    diff_after = {key: after[key] for key in diff_before}
    row.updated_at = timeutils.now()
    row.updated_by = actor
    row.save(update_fields=[*changes, "updated_at", "updated_by"])
    audit.record(
        action="settings.updated",
        entity_type="settings",
        entity_id="1",
        actor=actor,
        request=request,
        before=diff_before,
        after=diff_after,
    )
    return row
