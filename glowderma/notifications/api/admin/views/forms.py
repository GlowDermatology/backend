"""The multipart campaign form: un-dotting its keys and merging a PATCH into the stored values."""

from __future__ import annotations

from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.http import QueryDict

from glowderma.core import enums
from glowderma.notifications.models import Campaign


def form_to_nested(data: Any) -> dict[str, Any]:
    """Un-dot multipart keys (``audience.type`` → ``{"audience": {"type": …}}``); files are left out."""
    if not isinstance(data, QueryDict):
        return dict(data) if isinstance(data, dict) else {}
    nested: dict[str, Any] = {}
    for key in data:
        value: Any = data.get(key)
        if isinstance(value, UploadedFile):
            continue
        target = nested
        parts = key.split(".")
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = value
    return nested


MULTIPART_METHODS = frozenset({"POST", "PATCH"})


def stored_form(campaign: Campaign) -> dict[str, Any]:
    """The campaign's content as the multipart form would carry it (the baseline a PATCH is merged into)."""
    form: dict[str, Any] = {
        "kind": campaign.kind,
        "title": campaign.title,
        "body": campaign.body,
        "link": {"kind": campaign.link_kind},
        "audience": {"type": campaign.audience_type},
    }
    if campaign.link_kind == enums.LinkKind.PRODUCT:
        form["link"]["product_id"] = campaign.link_product_id
    elif campaign.link_kind == enums.LinkKind.URL:
        form["link"]["url"] = campaign.link_url
    if campaign.audience_type == enums.AudienceType.SEGMENT:
        form["audience"]["segment_id"] = campaign.audience_segment_id
    elif campaign.audience_type == enums.AudienceType.CUSTOMER:
        form["audience"]["phone"] = campaign.audience_phone
    if campaign.scheduled_at is not None:
        form["scheduled_at"] = campaign.scheduled_at.isoformat()
    return form


def merged_form(campaign: Campaign, submitted: dict[str, Any]) -> dict[str, Any]:
    """The stored form overlaid with the submitted keys: a submitted ``link`` or ``audience`` replaces the
    stored one as a whole (its kind decides which sub-keys matter); a submitted ``scheduled_at`` of ``""`` /
    ``null`` clears the requested time; a missing key keeps the stored value."""
    form = stored_form(campaign)
    for key in ("kind", "title", "body"):
        if key in submitted:
            form[key] = submitted[key]
    for key in ("link", "audience"):
        if key in submitted:
            form[key] = submitted[key]
    if "scheduled_at" in submitted:
        value = submitted["scheduled_at"]
        if value in ("", None):
            form.pop("scheduled_at", None)
        else:
            form["scheduled_at"] = value
    return form


__all__ = [
    "MULTIPART_METHODS",
    "form_to_nested",
    "merged_form",
    "stored_form",
]
