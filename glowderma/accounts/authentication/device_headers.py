"""The customer app's device headers (``X-GD-Device-Id`` / ``X-GD-Platform`` / ``X-GD-App-Version``, customer
contract §1.2): their validation, the minimum-version gate (426 ``app_update_required``) and where a parsed
set is kept on the request."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from rest_framework.request import Request

from glowderma.core import enums
from glowderma.core.api.exceptions import AppUpdateRequired, DeviceRequired
from glowderma.core.clientip import has_internal_secret
from glowderma.core.pii import pii_ref

# Customer contract §1.2.
FID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
APP_VERSION_PATTERN = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
PLATFORMS = frozenset(enums.OrderChannel.values)
# Where the parsed device headers are kept on the Django request (set by the customer base views).
REQUEST_DEVICE_ATTR = "gd_device"


@dataclass(frozen=True, slots=True)
class DeviceHeaders:
    fid: str
    platform: str
    app_version: str

    @property
    def fid_ref(self) -> str:
        return pii_ref(self.fid)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _store_url(platform: str) -> str:
    return str(settings.APP_STORE_URL if platform == enums.OrderChannel.IOS else settings.PLAY_STORE_URL)


def min_app_version(platform: str) -> str:
    return str(
        settings.MIN_APP_VERSION_IOS
        if platform == enums.OrderChannel.IOS
        else settings.MIN_APP_VERSION_ANDROID
    )


def parse_device_headers(request: Any, *, required: bool) -> DeviceHeaders | None:
    """Validate ``X-GD-Device-Id`` / ``X-GD-Platform`` / ``X-GD-App-Version`` (customer contract §1.2).

    Missing or malformed → 400 ``device_required`` when ``required`` (auth and C routes); on optional (O)
    routes no ``X-GD-Device-Id`` means no device (``None``), while a present one must come with valid
    companions. A version below the platform's minimum → 426 ``app_update_required`` with ``{min_version,
    store_url}``. Platform ``web`` (the website's BFF) is accepted only with a valid ``X-GD-Internal-Secret``
    (else 400 ``device_required``); its ``X-GD-App-Version`` is optional (validated when present, ``""``
    when absent) and no minimum version applies.
    """
    headers = request.headers
    fid = str(headers.get("X-GD-Device-Id", ""))
    if not fid and not required:
        return None
    platform = str(headers.get("X-GD-Platform", ""))
    version = str(headers.get("X-GD-App-Version", ""))
    if platform == enums.OrderChannel.WEB and not version:
        # The website (B-W1) has no installed app: the version header is optional and never has a floor.
        version = ""
    elif not APP_VERSION_PATTERN.fullmatch(version):
        raise DeviceRequired()
    if not FID_PATTERN.fullmatch(fid) or platform not in PLATFORMS:
        raise DeviceRequired()
    if platform == enums.OrderChannel.WEB and not has_internal_secret(getattr(request, "_request", request)):
        # only the website's server-side BFF (with the internal secret) is "web": a self-asserted web
        # platform would skip the app's minimum-version floor
        raise DeviceRequired()
    if platform == enums.OrderChannel.WEB:
        return DeviceHeaders(fid=fid, platform=platform, app_version=version)
    floor = min_app_version(platform)
    if _version_tuple(version) < _version_tuple(floor):
        raise AppUpdateRequired(details={"min_version": floor, "store_url": _store_url(platform)})
    return DeviceHeaders(fid=fid, platform=platform, app_version=version)


def request_device(request: Request) -> DeviceHeaders | None:
    """The device headers the customer base view parsed for this request (``None`` when absent)."""
    device = getattr(request._request, REQUEST_DEVICE_ATTR, None)
    return device if isinstance(device, DeviceHeaders) else None


__all__ = [
    "APP_VERSION_PATTERN",
    "FID_PATTERN",
    "PLATFORMS",
    "REQUEST_DEVICE_ATTR",
    "DeviceHeaders",
    "min_app_version",
    "parse_device_headers",
    "request_device",
]
