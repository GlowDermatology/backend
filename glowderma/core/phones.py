"""Lenient reading of an Uzbek mobile number typed or pasted by staff (admin panel, feat/admin-ux).

Staff paste numbers from spreadsheets, messengers and call logs: ``+998 90 123-45-67``, ``998901234567``,
``(90) 123 45 67``, ``0099890…``, ``90 123 45 67``. :func:`normalize_uz_phone` turns every such spelling
into the contract's E.164 form ``+998XXXXXXXXX`` (admin contract §1.4) and refuses anything else; the
admin serializers that take a phone use it through ``core.api.fields.LenientPhoneField``. The customer
API keeps the strict field: a customer's app always sends E.164.

Accepted: ASCII digits, separators (spaces of any kind, ``-`` and the Unicode dashes, ``.``, ``(``, ``)``,
``/``), one leading ``+``; then exactly 9 national digits, optionally after ``998``, ``+998`` or
``00998``. Refused: letters, any other character, another country code, a wrong digit count, input longer
than :data:`MAX_INPUT_LENGTH`. The number is never logged.
"""

from __future__ import annotations

import re
from typing import Final

MAX_INPUT_LENGTH: Final = 32
COUNTRY_CODE: Final = "998"
NATIONAL_DIGITS: Final = 9
E164_PATTERN: Final = re.compile(r"\+998\d{9}")

# spaces (incl. no-break / thin / ideographic ones), ASCII and Unicode dashes, dots, brackets, slashes
_SEPARATORS = re.compile(r"[\s  -   　\-‐-―−.()/]")
_DIGITS = re.compile(r"[0-9]+")


def normalize_uz_phone(raw: object) -> str | None:
    """``"+998901234567"`` for any accepted spelling of that number, else ``None``."""
    if not isinstance(raw, str) or len(raw) > MAX_INPUT_LENGTH:
        return None
    text = raw.strip()
    plus = text.startswith("+")
    body = _SEPARATORS.sub("", text[1:] if plus else text)
    if not body or _DIGITS.fullmatch(body) is None:  # letters, a second "+", anything else
        return None
    if plus:
        if not body.startswith(COUNTRY_CODE):
            return None
        national = body[len(COUNTRY_CODE) :]
    elif body.startswith("00" + COUNTRY_CODE):
        national = body[len(COUNTRY_CODE) + 2 :]
    elif len(body) == len(COUNTRY_CODE) + NATIONAL_DIGITS and body.startswith(COUNTRY_CODE):
        national = body[len(COUNTRY_CODE) :]
    else:
        national = body
    if len(national) != NATIONAL_DIGITS:
        return None
    return f"+{COUNTRY_CODE}{national}"
