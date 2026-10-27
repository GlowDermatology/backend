"""City-level location of a client IP from a **local** GeoIP City database (B-W1, customer contract §2.2.10).

Compliance: no foreign lookup service is ever called. The operator mounts a MaxMind GeoLite2-City or DB-IP
City Lite ``.mmdb`` file read-only and points ``GEOIP_CITY_DB`` at it (refreshed monthly); unset, missing or
unreadable → every lookup is ``""`` (the API shows "—"). The reader is opened once per path and process
(``MODE_AUTO``: memory-mapped when available), shared by threads (the reader is thread-safe) and reopened
when the file is replaced (its mtime is checked at most once a minute; a failed open is retried after a
minute).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import maxminddb
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_LENGTH = 120

# The database file is replaced monthly by the operator: its mtime is checked at most this often, and a
# failed open is retried after the same delay (never on every request).
CHECK_SECONDS = 60.0

_lock = threading.Lock()
_clock = time.monotonic  # module attribute: tests replace it


@dataclass(slots=True)
class _Entry:
    reader: Any | None  # None = the last open failed
    mtime: float
    checked: float


_entries: dict[str, _Entry] = {}


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return -1.0


def _open(path: str) -> Any | None:
    try:
        return maxminddb.open_database(path)
    except OSError, ValueError, maxminddb.InvalidDatabaseError:
        logger.warning("geoip.unavailable: GEOIP_CITY_DB cannot be opened")
        return None


def _close(reader: Any | None) -> None:
    if reader is None:
        return
    try:
        reader.close()
    except Exception:  # noqa: BLE001 - closing is best effort
        logger.debug("geoip reader close failed", exc_info=True)


def _reader() -> Any | None:
    """The shared reader of ``GEOIP_CITY_DB``; reopened when the file's mtime changed (checked at most every
    ``CHECK_SECONDS``), a failed open retried after ``CHECK_SECONDS``."""
    path = str(getattr(settings, "GEOIP_CITY_DB", "") or "")
    if not path:
        return None
    now = _clock()
    entry = _entries.get(path)
    if entry is not None and now - entry.checked < CHECK_SECONDS:
        return entry.reader
    with _lock:
        entry = _entries.get(path)
        if entry is not None and now - entry.checked < CHECK_SECONDS:
            return entry.reader
        mtime = _mtime(path)
        if entry is not None and entry.reader is not None and mtime == entry.mtime:
            entry.checked = now
            return entry.reader
        reader = _open(path)
        if entry is not None and entry.reader is not None and reader is None:
            # the replacement cannot be opened (half-copied?): keep serving the old one, try again later
            entry.checked = now
            return entry.reader
        if entry is not None:
            _close(entry.reader)
        _entries[path] = _Entry(reader=reader, mtime=mtime, checked=now)
        return reader


def reset() -> None:
    """Forget the opened readers (tests, a replaced database file)."""
    with _lock:
        for entry in _entries.values():
            _close(entry.reader)
        _entries.clear()


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
