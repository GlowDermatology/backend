"""The signed-in customer's profile, consent and image (customer contract §2.2.2–§2.2.5).

Every signed-in write takes ``accounts.services.lock_signed_in_customer`` (customer active, the request's
session still open) before writing, so a request authenticated before a concurrent ``DELETE me/`` or reset
committed writes nothing to the deleted or taken-over account (C§6.17)."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import media_name, url_fields
from glowderma.customers.services.common import passwords, published_terms, trail
from glowderma.site_config import selectors as site_config_selectors

logger = logging.getLogger("glowderma.customers.services")


def accept_consent(
    customer_id: int,
    *,
    terms_version: str,
    ip: str,
    sid: str | account_services.NoSession,
    request: Any = None,
) -> None:
    terms = site_config_selectors.current_terms()
    if not terms.published:
        raise passwords.field_refusal("terms_version", "terms_outdated", "field.terms_outdated")
    published_terms.check_terms_version(terms_version, terms)
    with transaction.atomic():
        customer = account_services.lock_signed_in_customer(customer_id, sid)
        account_services.stamp_consent(customer_id, terms_version=terms_version, ip=ip)
        trail.audit_customer("customer.consent_accepted", customer, request, {"terms_version": terms_version})


def update_profile(
    customer_id: int, changes: dict[str, Any], *, fid: str, sid: str | account_services.NoSession
) -> None:
    """``PATCH me/``: name and language on the customer, ``push_enabled`` on the current device (requires a
    stored FCM token: ``push_token_missing``). No audit (own profile)."""
    fields = {name: changes[name] for name in ("name", "language", "push_enabled") if name in changes}
    with transaction.atomic():
        account_services.lock_signed_in_customer(customer_id, sid)
        account_services.update_customer_profile(customer_id, fid=fid, **fields)
    logger.info("customer.profile_updated customer_id=%s fields=%s", customer_id, sorted(changes))


def set_image(
    customer_id: int, upload: Any, *, sid: str | account_services.NoSession
) -> dict[str, str | None]:
    """``PUT me/image/``: re-encoded and EXIF-stripped (``core.images``), the previous file deleted. Returns
    ``{image_url, image_thumbnail_url, image_medium_url}``."""
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise passwords.field_refusal("file", exc.code) from exc
    with transaction.atomic():
        customer = account_services.lock_signed_in_customer(customer_id, sid)
        name = media_name("customers", customer_id, processed.content, processed.extension)
        new_name = account_services.set_customer_image(customer, PurePosixPath(name).name, processed.content)
    return url_fields("image", new_name)


def delete_image(customer_id: int, *, sid: str | account_services.NoSession) -> None:
    with transaction.atomic():
        account_services.lock_signed_in_customer(customer_id, sid)
        account_services.clear_customer_image(customer_id)


__all__ = [
    "accept_consent",
    "delete_image",
    "set_image",
    "update_profile",
]
