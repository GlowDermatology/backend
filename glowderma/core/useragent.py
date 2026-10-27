"""A small user-agent reader for the customer session list (B-W1, customer contract §2.2.10).

It only names the browser family and the operating system ("Chrome (Windows)", "Safari (Mac OS X)") — enough
for a person to recognise their own devices; it never drives behaviour. Order matters: browsers built on
Chromium or WebKit also carry the ``Chrome/`` and ``Safari/`` tokens, so the specific ones are checked first.
No dependency: a handful of well-known tokens covers the browsers seen in Uzbekistan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (browser name, token pattern) — first match wins.
_BROWSERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Edge", re.compile(r"\b(?:Edg|EdgA|EdgiOS|Edge)/\d")),
    ("Opera", re.compile(r"\b(?:OPR|OPiOS|OPT|Opera)/\d|\bOpera\b")),
    ("Yandex Browser", re.compile(r"\bYaBrowser/\d|\bYaSearchBrowser/\d")),
    ("Samsung Internet", re.compile(r"\bSamsungBrowser/\d")),
    ("Telegram", re.compile(r"\bTelegram-Android/\d|\bTelegramBot\b")),
    ("Instagram", re.compile(r"\bInstagram \d")),
    ("Facebook", re.compile(r"\b(?:FBAN|FBAV|FB_IAB)/")),
    ("Firefox", re.compile(r"\b(?:Firefox|FxiOS)/\d")),
    ("Chrome", re.compile(r"\b(?:Chrome|CriOS|Chromium)/\d")),
    ("Safari", re.compile(r"\bVersion/\d[^ ]* (?:Mobile/\w+ )?Safari/\d")),
)

# (OS name, token pattern) — first match wins (iOS before Mac OS X: iPad desktop mode still says iPad
# when it can; Android before Linux).
_SYSTEMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Windows", re.compile(r"\bWindows(?: NT| Phone)?\b")),
    ("iOS", re.compile(r"\b(?:iPhone|iPad|iPod)\b")),
    ("Android", re.compile(r"\bAndroid\b")),
    ("Chrome OS", re.compile(r"\bCrOS\b")),
    ("Mac OS X", re.compile(r"\bMac OS X\b|\bMacintosh\b")),
    ("Linux", re.compile(r"\bLinux\b|\bX11\b")),
)


@dataclass(frozen=True, slots=True)
class UserAgent:
    browser: str  # "" when unknown
    os: str  # "" when unknown


def _first(table: tuple[tuple[str, re.Pattern[str]], ...], value: str) -> str:
    for name, pattern in table:
        if pattern.search(value):
            return name
    return ""


def parse(value: str) -> UserAgent:
    """The browser family and operating system named by ``value`` (``""`` for each one not recognised)."""
    value = (value or "")[:1024]
    return UserAgent(browser=_first(_BROWSERS, value), os=_first(_SYSTEMS, value))


def label(value: str, *, unknown: str) -> str:
    """``"Browser (OS)"``, ``"Browser"`` or ``"<unknown> (OS)"``; ``unknown`` alone when nothing is known."""
    agent = parse(value)
    browser = agent.browser or unknown
    return f"{browser} ({agent.os})" if agent.os else browser
