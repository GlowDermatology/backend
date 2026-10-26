"""Cashier terminal authentication (customer contract §2.10): ``Authorization: Api-Key <key>``.

The key (``gdpos_<8-char prefix>_<32 base64url chars>``) is looked up by its SHA-256 against
``PosDevice.key_hash`` and compared with ``compare_digest``; the device must be active; ``last_used_at`` moves
at most once per minute. The raw key is never logged.

Failed key authentications (missing, malformed, unknown or inactive key) are counted per client IP in the
cache (DRF authenticates before it throttles, so the ``pos`` scope never sees them): beyond
``POS_FAILED_AUTH_PER_IP_PER_MIN`` failures in the current minute the request is answered 429 ``throttled``
**before** any lookup.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import Throttled
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from glowderma.core import timeutils
from glowderma.loyalty.models import PosDevice

if TYPE_CHECKING:
    from rest_framework.views import APIView

logger = logging.getLogger(__name__)

SCHEME = "Api-Key"
AUDIENCE = "pos"
FAILED_WINDOW_SECONDS = 60
LAST_USED_RESOLUTION = timedelta(minutes=1)


def key_digest(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str:
    return str(getattr(request, "client_ip", None) or request.META.get("REMOTE_ADDR", ""))


def _failure_key(request: Request) -> str:
    """A per-IP counter whose one-minute window starts with the first failure (``cache.add`` + TTL)."""
    return f"pos:authfail:{_client_ip(request)}"


def _failures(request: Request) -> int:
    return int(cache.get(_failure_key(request), 0) or 0)


def _record_failure(request: Request) -> None:
    key = _failure_key(request)
    cache.add(key, 0, FAILED_WINDOW_SECONDS)
    try:
        cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, FAILED_WINDOW_SECONDS)


def _presented_key(request: Request) -> str | None:
    """The key of an ``Api-Key`` header, or ``None`` (absent, another scheme, malformed)."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not isinstance(header, str):
        return None
    scheme, _sep, value = header.strip().partition(" ")
    if scheme.lower() != SCHEME.lower():
        return None
    value = value.strip()
    if not value or " " in value or len(value) > 128:
        return None
    return value


def authenticate_pos_key(raw_key: str) -> PosDevice | None:
    """The active device whose key hash matches ``raw_key`` (``compare_digest``), else ``None``; touches
    ``last_used_at`` at most once per minute."""
    digest = key_digest(raw_key)
    device = PosDevice.objects.filter(key_hash=digest).first()
    if device is None or not hmac.compare_digest(device.key_hash, digest) or not device.is_active:
        return None
    instant = timeutils.now()
    stale = Q(last_used_at__isnull=True) | Q(last_used_at__lt=instant - LAST_USED_RESOLUTION)
    if PosDevice.objects.filter(stale, pk=device.pk).update(last_used_at=instant):
        device.last_used_at = instant
    return device


class PosKeyAuthentication(BaseAuthentication):
    """``Authorization: Api-Key <key>`` → ``(PosDevice, {"aud": "pos", "pos_device_id": id})``.

    Every request without a valid key counts one failure for its IP; over the limit → 429 before a lookup.
    A missing or invalid key returns ``None``; ``IsActivePosDevice`` then answers 401 (``WWW-Authenticate:
    Api-Key``).
    """

    def authenticate(self, request: Request) -> tuple[PosDevice, dict[str, Any]] | None:
        limit = int(settings.POS_FAILED_AUTH_PER_IP_PER_MIN)
        if _failures(request) >= limit:
            raise Throttled(wait=FAILED_WINDOW_SECONDS)
        raw_key = _presented_key(request)
        device = authenticate_pos_key(raw_key) if raw_key is not None else None
        if device is None:
            _record_failure(request)
            logger.info("pos.auth_failed ip=%s", _client_ip(request))
            return None
        return device, {"aud": AUDIENCE, "pos_device_id": device.pk}

    def authenticate_header(self, request: Request) -> str:
        return SCHEME


class PosApiKeyScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    """OpenAPI security scheme of :class:`PosKeyAuthentication`: ``Authorization: Api-Key <key>``."""

    target_class = "glowderma.loyalty.authentication.PosKeyAuthentication"
    name = "PosApiKey"

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, Any]:
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "`Api-Key gdpos_<prefix>_<secret>` (one key per cashier terminal).",
        }


class IsActivePosDevice(BasePermission):
    """A request authenticated by :class:`PosKeyAuthentication` with an active device."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        device: object = request.user
        return (
            isinstance(device, PosDevice)
            and device.is_active
            and isinstance(request.auth, dict)
            and request.auth.get("aud") == AUDIENCE
        )
