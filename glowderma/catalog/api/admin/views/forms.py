"""The multipart forms of categories and banners: un-dotting their keys (an empty string means ``null`` for
the nullable banner keys) and the optional image part."""

from __future__ import annotations

from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.http import QueryDict
from rest_framework.request import Request


def category_image(request: Request) -> Any:
    """The optional ``image`` part (multipart only; a JSON body carries none)."""
    return request.FILES.get("image") if request.FILES else None


# Multipart keys whose empty string means ``null`` (see BannerWriteSerializer's docstring).
NULLABLE_FORM_KEYS = frozenset({"link.product_id", "link.category", "link.url", "starts_at", "ends_at"})


def form_to_nested(data: Any) -> dict[str, Any]:
    """Un-dot multipart keys (``title.uz`` → ``{"title": {"uz": …}}``); JSON bodies pass through."""
    if not isinstance(data, QueryDict):
        return dict(data) if isinstance(data, dict) else {}
    nested: dict[str, Any] = {}
    for key in data:
        # DRF merges request.FILES into request.data, so a value may be a file although the QueryDict stubs
        # only know strings: widen the type so the guard is not flagged as unreachable.
        value: object = data.get(key)
        if isinstance(value, UploadedFile):
            continue
        if key in NULLABLE_FORM_KEYS and value == "":
            value = None
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


__all__ = [
    "NULLABLE_FORM_KEYS",
    "category_image",
    "form_to_nested",
]
