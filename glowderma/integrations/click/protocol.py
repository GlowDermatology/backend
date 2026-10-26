"""Click SHOP API protocol helpers (customer contract §2.13.1) — pure functions and constants, no database.

The webhook views of ``commerce/api/webhooks/click.py`` call these before taking any lock: required
parameters,
the MD5 signature over the **raw received strings**, the amount format and the semantic fingerprint used for
the
duplicate check. Every choice marked *verify M3* in the contract sits behind a constant here.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlencode

ACTION_PREPARE: Final = "0"
ACTION_COMPLETE: Final = "1"

OK: Final = 0
SIGN_FAILED: Final = -1
BAD_AMOUNT: Final = -2
ACTION_NOT_FOUND: Final = -3
ALREADY_PAID: Final = -4
USER_NOT_FOUND: Final = -5
TRANSACTION_NOT_FOUND: Final = -6
UPDATE_FAILED: Final = -7  # reserved: a database failure answers HTTP 500 so Click retries
BAD_REQUEST: Final = -8
TRANSACTION_CANCELLED: Final = -9

# ``error_note`` texts as Click documents them (customer contract §2.13.1).
ERRORS: Final[dict[int, str]] = {
    OK: "Success",
    SIGN_FAILED: "SIGN CHECK FAILED!",
    BAD_AMOUNT: "Incorrect parameter amount",
    ACTION_NOT_FOUND: "Action not found",
    ALREADY_PAID: "Already paid",
    USER_NOT_FOUND: "User does not exist",
    TRANSACTION_NOT_FOUND: "Transaction does not exist",
    UPDATE_FAILED: "Failed to update user",
    BAD_REQUEST: "Error in request from click",
    TRANSACTION_CANCELLED: "Transaction cancelled",
}
# Content type switch (verify M3): when the sandbox posts JSON, it is parsed with
# parse_int=str/parse_float=str and
# string values are required, so the signed strings are never re-serialised.
CLICK_ACCEPT_JSON: Final = False
# What an unexpected exception answers: HTTP 500 (everything rolled back; Click retries).
INTERNAL_ERROR_RESPONSE: Final = "http_500"

PREPARE_PARAMS: Final = (
    "click_trans_id",
    "service_id",
    "click_paydoc_id",
    "merchant_trans_id",
    "amount",
    "action",
    "error",
    "error_note",
    "sign_time",
    "sign_string",
)
COMPLETE_PARAMS: Final = (*PREPARE_PARAMS[:4], "merchant_prepare_id", *PREPARE_PARAMS[4:])
FINGERPRINT_FIELDS: Final = (
    "click_trans_id",
    "service_id",
    "merchant_trans_id",
    "merchant_prepare_id",
    "amount",
    "action",
    "error",
)
_INT = re.compile(r"-?[0-9]{1,19}")
_AMOUNT = re.compile(r"(0|[1-9][0-9]{0,15})(?:\.([0-9]{1,2}))?")
_SIGN_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def missing_or_malformed(params: Mapping[str, str], *, complete: bool) -> bool:
    """Any required parameter missing, or an integer parameter that is not an integer (→ ``-8``)."""
    names = COMPLETE_PARAMS if complete else PREPARE_PARAMS
    if any(name not in params for name in names if name != "error_note"):
        return True
    integers = ["click_trans_id", "service_id", "click_paydoc_id", "action", "error"]
    if complete:
        integers.append("merchant_prepare_id")
    if any(not _INT.fullmatch(params[name]) for name in integers):
        return True
    return not params["merchant_trans_id"] or not params["sign_string"] or not params["sign_time"]


def signature(params: Mapping[str, str], secret: str, *, complete: bool) -> str:
    """Lower-case hex MD5 over the raw strings (Prepare / Complete formula of §2.13.1)."""
    parts = [params.get("click_trans_id", ""), params.get("service_id", ""), secret]
    parts.append(params.get("merchant_trans_id", ""))
    if complete:
        parts.append(params.get("merchant_prepare_id", ""))
    parts += [params.get("amount", ""), params.get("action", ""), params.get("sign_time", "")]
    return hashlib.md5("".join(parts).encode(), usedforsecurity=False).hexdigest()


def sign_ok(params: Mapping[str, str], secret: str, *, complete: bool) -> bool:
    """``compare_digest`` of the received ``sign_string`` with :func:`signature`."""
    received = params.get("sign_string", "").lower()
    return bool(secret) and hmac.compare_digest(received, signature(params, secret, complete=complete))


def parse_amount(value: str) -> int | None:
    """Integral so'm from a non-negative decimal string with ≤ 2 dp (``"784400"``, ``"784400.00"``); ``None``
    for anything else (``"784400.50"`` — not integral —, ``"abc"``, ``"-1"``)."""
    match = _AMOUNT.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    if match.group(2) and int(match.group(2)) != 0:
        return None
    return int(match.group(1))


def fingerprint(params: Mapping[str, str]) -> str:
    """SHA-256 over the semantic fields (never ``sign_time``, ``sign_string``, ``error_note``,
    ``click_paydoc_id``): a plain or re-signed retry has the same fingerprint, a conflicting replay does
    not."""
    canonical = "\x1f".join(f"{name}={params.get(name, '')}" for name in FINGERPRINT_FIELDS)
    return hashlib.sha256(canonical.encode()).hexdigest()


def response(
    params: Mapping[str, str], error: int, *, complete: bool, payment_id: int | None = None
) -> dict[str, object]:
    """The provider-native answer; ``merchant_prepare_id`` / ``merchant_confirm_id`` is null on errors."""
    click_trans_id: object = params.get("click_trans_id", "")
    if isinstance(click_trans_id, str) and _INT.fullmatch(click_trans_id):
        click_trans_id = int(click_trans_id)
    body: dict[str, object] = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": params.get("merchant_trans_id", ""),
        "merchant_confirm_id" if complete else "merchant_prepare_id": payment_id if error == OK else None,
        "error": error,
        "error_note": ERRORS[error],
    }
    return body


def pay_url(
    base: str, *, service_id: str, merchant_id: str, amount: int, account_ref: str, return_url: str
) -> str:
    """``{CLICK_PAY_URL}?service_id=…&merchant_id=…&amount=…&transaction_param=…&return_url=…`` (§2.7.2)."""
    query = urlencode(
        {
            "service_id": service_id,
            "merchant_id": merchant_id,
            "amount": str(amount),
            "transaction_param": account_ref,
            "return_url": return_url,
        }
    )
    return f"{base}?{query}"


def valid_sign_time(value: str) -> bool:
    return bool(_SIGN_TIME.fullmatch(value))
