"""Click SHOP API protocol helpers (customer contract §2.13.1) — pure functions and constants, no database.

The webhook views of ``commerce/api/webhooks/click.py`` call these before taking any lock: required
parameters, the MD5 signature over the **raw received strings**, the amount format and the semantic
fingerprint used for the duplicate check. Every choice marked *verify M3* in the contract sits behind a
constant here.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Final, Literal
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
BIGINT_MAX: Final = 9_223_372_036_854_775_807
_AMOUNT = re.compile(r"(0|[1-9][0-9]{0,15})(?:\.([0-9]{1,2}))?")
_SIGN_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def missing_or_malformed(params: Mapping[str, str], *, complete: bool) -> bool:
    """Any required parameter missing, or an integer parameter that is not an integer (→ ``-8``)."""
    names = COMPLETE_PARAMS if complete else PREPARE_PARAMS
    if any(name not in params for name in names if name != "error_note"):
        return True
    ids = ["click_trans_id", "service_id", "click_paydoc_id"]
    if complete:
        ids.append("merchant_prepare_id")
    integers = [*ids, "action", "error"]
    if any(not _INT.fullmatch(params[name]) or abs(int(params[name])) > BIGINT_MAX for name in integers):
        return True  # beyond bigint a lookup would raise DataError (HTTP 500, Click retrying forever)
    if any(int(params[name]) < 0 for name in ids):
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
    expected = signature(params, secret, complete=complete)
    # bytes: ``compare_digest`` raises TypeError on a ``str`` with non-ASCII characters (HTTP 500)
    return bool(secret) and hmac.compare_digest(received.encode(), expected.encode())


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


# --- Merchant API (outbound: status check and reversal; customer contract §2.13.0) --------------------------
# Source: docs.click.uz/en/merchant-api/requests (read 2026-10-26). Requests carry ``Accept`` /
# ``Content-Type: application/json`` and ``Auth: merchant_user_id:sha1(timestamp + secret_key):timestamp``
# (10-digit UNIX seconds). The secret is the one ``secret_key`` Click issues per service (the SHOP API's
# ``CLICK_SECRET_KEY``; Click's own PHP library configures that one key for both). HTTP statuses
# (docs.click.uz/en/merchant-api/errors): 200 and 201 OK, 400, 401, 403, 404, 406, 410, 500, 502; a
# processed request answers 2xx with ``error_code`` (0 = success) and ``error_note``.
MERCHANT_API_OK: Final = 0
MERCHANT_PATH_STATUS: Final = "payment/status/{service_id}/{payment_id}"
MERCHANT_PATH_REVERSAL: Final = "payment/reversal/{service_id}/{payment_id}"
# ``payment_status`` codes: docs.click.uz/en/merchant-api/click-pass ("Payment Status Codes") documents
# ``< 0`` error, ``0`` created, ``1`` processing, ``2`` successful. Only ``2`` reads ``paid``. ``-99`` reads
# ``reversed`` — a guess (verify M3): -99 is the invoice status "Deleted" in Click's docs and what Click's PHP
# library (github.com/click-llc/click-integration-php, ``BasePayments::on_invoice_checked``) treats as
# cancelled; a reversal's code must be confirmed on the sandbox. Every other code (``1`` processing included)
# is "unknown" (ClickStatusUnknown), so an unverified code can never close or skip a refund.
MERCHANT_STATUS_PAID: Final = 2
MERCHANT_STATUS_REVERSED: Final = frozenset({-99})
# Click reversal conditions (docs.click.uz): a completed payment of the current reporting month (the previous
# month only on the 1st), made with an online card; UZCARD may refuse.


def merchant_auth_header(merchant_user_id: str, secret_key: str, timestamp: int) -> str:
    """``merchant_user_id:sha1(timestamp + secret_key):timestamp`` (lower-case hex digest)."""
    digest = hashlib.sha1(f"{timestamp}{secret_key}".encode(), usedforsecurity=False).hexdigest()
    return f"{merchant_user_id}:{digest}:{timestamp}"


def merchant_payment_state(code: object) -> Literal["paid", "reversed"] | None:
    """The port state of a Merchant API ``payment_status``: ``2`` → ``paid``, ``-99`` → ``reversed`` (see
    :data:`MERCHANT_STATUS_REVERSED`), anything else ``None`` (unknown: never treated as paid or reversed)."""
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        value = code
    elif isinstance(code, str) and re.fullmatch(r"-?[0-9]{1,6}", code):
        value = int(code)
    else:
        return None
    if value in MERCHANT_STATUS_REVERSED:
        return "reversed"
    if value == MERCHANT_STATUS_PAID:
        return "paid"
    return None
