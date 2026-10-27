"""City-level location of a client IP from a **local** GeoIP City database (B-W1, customer contract §2.2.10).

Compliance: no foreign lookup service is ever called. The operator mounts a MaxMind GeoLite2-City or DB-IP
City Lite ``.mmdb`` file read-only and points ``GEOIP_CITY_DB`` at it (refreshed monthly); unset, missing or
unreadable → every lookup is ``""`` (the API shows "—"). The reader is opened once per path and process
(``MODE_AUTO``: memory-mapped when available) and shared by threads (the reader is thread-safe).
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from typing import Any

import maxminddb
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_LENGTH = 120

_lock = threading.Lock()
_readers: dict[str, Any] = {}
_failed: set[str] = set()


def _reader() -> Any | None:
    path = str(getattr(settings, "GEOIP_CITY_DB", "") or "")
    if not path or path in _failed:
        return None
    reader = _readers.get(path)
    if reader is not None:
        return reader
    with _lock:
        reader = _readers.get(path)
        if reader is None and path not in _failed:
            try:
                reader = maxminddb.open_database(path)
            except OSError, ValueError, maxminddb.InvalidDatabaseError:
                logger.warning("geoip.unavailable: GEOIP_CITY_DB cannot be opened")
                _failed.add(path)
                return None
            _readers[path] = reader
    return reader


def reset() -> None:
    """Forget the opened readers and failed paths (tests, a replaced database file)."""
    with _lock:
        for reader in _readers.values():
            try:
                reader.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                logger.debug("geoip reader close failed", exc_info=True)
        _readers.clear()
        _failed.clear()


def _name(node: Any, language: str) -> str:
    if not isinstance(node, dict):
        return ""
    names = node.get("names")
    if not isinstance(names, dict):
        return ""
    value = names.get(language) or names.get("en") or ""
    return str(value).strip()


def describe(record: Any, language: str = "en") -> str:
    """``"City, Subdivision, CC"`` from a City record ("Tashkent, Tashkent, UZ"; missing parts are left
    out); ``""`` when the record names no place."""
    if not isinstance(record, dict):
        return ""
    city = _name(record.get("city"), language)
    subdivisions = record.get("subdivisions")
    region = _name(subdivisions[0], language) if isinstance(subdivisions, list) and subdivisions else ""
    country = record.get("country")
    code = str(country.get("iso_code") or "").strip() if isinstance(country, dict) else ""
    return ", ".join(part for part in (city, region, code) if part)[:MAX_LENGTH]


def locate(ip: str, language: str = "en") -> str:
    """The place of a public IP (``""`` for a private, reserved or unknown address, or without a
    database). Never raises."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    if not address.is_global:
        return ""
    reader = _reader()
    if reader is None:
        return ""
    try:
        record = reader.get(str(address))
    except Exception:  # noqa: BLE001 - a corrupt database must never fail a request
        logger.warning("geoip.lookup_failed", exc_info=True)
        return ""
    return describe(record, language)
