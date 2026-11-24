"""How a Billz answer becomes an error: the business codes and their reasons, the scrubbed details and the
classification (retryable, shift closed, configuration, permanent)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from glowderma.core.pii import clean_text, scrub_free_text
from glowderma.integrations.billz.ports import (
    BillzError,
    BillzMisconfigured,
    BillzRejected,
    BillzShiftClosed,
    BillzUnavailable,
)

# Billz's own free text kept in an error (after scrubbing).
MESSAGE_LIMIT = 300


CODE_LIMIT = 64


# Business codes of the sale methods (llms-full "Коды ответов и ошибки", "Интернет-заказ" step 7). Retryable:
# the next attempt can succeed on its own (an async recalculation still running, a bill that changed under
# us, items an async add has not applied yet). The closed shift waits for the next shift (decision D2); a
# configuration-type code waits for staff to fix a setting (decision D3). Everything else — and every unknown
# code — is permanent: staff must act (fix the product, check the sale) and the outbox message goes dead.
ERROR_REASONS: dict[int, str] = {
    20009: "the cash box belongs to another shop than the sale",
    20010: "no open cash box in the sale's shop (shift closed)",
    20011: "the cash box has not enough money",
    20015: "the sale has no items",
    20016: "not enough stock in the sale's shop",
    20017: "the sale was deleted in Billz",
    20020: "the payments do not match the bill total",
    20033: "no open shift",
    20036: "product not found in Billz",
    20037: "the product is archived in Billz",
    20047: "not enough customer cashback balance",
    20063: "the sale was not sent to the fiscal operator (OFD)",
    20082: "payment refused: the cash box is closed",
    20084: "the bill is not calculated yet",
    20085: "the seller has no access to the shop",
}


RETRYABLE_ERROR_CODES = frozenset({20015, 20020, 20084})


SHIFT_CLOSED_CODES = frozenset({20010, 20033, 20082})


CONFIGURATION_ERROR_CODES = frozenset({20009, 20085})


RETRYABLE_STRING_CODES = frozenset({"TIMEOUT", "TOO_MANY_REQUESTS", "rate_limited", "server_error"})


class BillzAuthError(BillzMisconfigured):
    """Billz refused the integration key (login 4xx): an operator must check ``BILLZ_SECRET_KEY``."""

    code = "billz_auth_failed"


def json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def error_details(body: Mapping[str, Any]) -> tuple[int | None, str, str]:
    """``(business code, string code, message)`` of any of the three Billz error bodies; the texts are
    scrubbed (review B13: Billz may echo a phone number, a NUL byte would break the dead-letter write)."""
    data = body.get("data")
    business = int_or_none(data.get("error_code")) if isinstance(data, Mapping) else None
    error = body.get("error")
    string_code = ""
    message = ""
    if isinstance(error, Mapping):
        string_code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    elif isinstance(error, str):
        string_code = error
    if isinstance(data, Mapping) and data.get("message"):
        message = str(data["message"])
    elif not message and isinstance(body.get("message"), str):
        message = str(body["message"])
    return business, clean_text(string_code, CODE_LIMIT), scrub_free_text(message, MESSAGE_LIMIT)


def classify(label: str, http_status: int, body: Mapping[str, Any], *, resource: bool = False) -> BillzError:
    """The typed error of a failed call (``label`` = ``"POST /v2/order-payment"``, no ids, no query).

    ``resource``: the call reads one resource by id (``GET /v2/order/{id}``), so a 404 means that resource is
    gone (permanent); on every other route a 404 means the route itself is wrong (configuration, D3).
    """
    business, string_code, message = error_details(body)
    if business is not None:
        reason = ERROR_REASONS.get(business, message or "unknown Billz error")
        text = f"{label}: {reason} (Billz error {business})"
        if business in RETRYABLE_ERROR_CODES:
            return BillzUnavailable(text, http_status=http_status, error_code=business, not_applied=True)
        if business in SHIFT_CLOSED_CODES:
            return BillzShiftClosed(text, http_status=http_status, error_code=business, not_applied=True)
        if business in CONFIGURATION_ERROR_CODES:
            return BillzMisconfigured(text, http_status=http_status, error_code=business, not_applied=True)
        return BillzRejected(f"{label}: {reason}", error_code=business, http_status=http_status)
    if http_status in (408, 429) or http_status >= 500 or string_code in RETRYABLE_STRING_CODES:
        # 429 / TOO_MANY_REQUESTS: Billz refused before doing anything; 408 / 5xx / TIMEOUT: unknown outcome
        refused = http_status == 429 or string_code in ("TOO_MANY_REQUESTS", "rate_limited")
        return BillzUnavailable(
            f"{label}: HTTP {http_status} {string_code}".rstrip(),
            http_status=http_status,
            not_applied=refused,
        )
    if 300 <= http_status < 400:
        return BillzMisconfigured(
            f"{label}: Billz answered a redirect (HTTP {http_status}); check BILLZ_BASE_URL",
            http_status=http_status,
            not_applied=True,
        )
    if http_status == 403:
        detail = string_code or message or "forbidden"
        return BillzMisconfigured(
            f"{label}: the integration key may not call this route ({detail}) [HTTP 403]",
            http_status=http_status,
            not_applied=True,
        )
    if http_status == 404:
        if resource:
            return BillzRejected(f"{label}: not found", http_status=http_status)
        detail = message or string_code or "not found"
        return BillzMisconfigured(
            f"{label}: {detail} [HTTP 404]; check BILLZ_BASE_URL and the routes granted to the key",
            http_status=http_status,
            not_applied=True,
        )
    detail = message or string_code or "refused"
    return BillzRejected(f"{label}: {detail}", http_status=http_status)


__all__ = [
    "CODE_LIMIT",
    "CONFIGURATION_ERROR_CODES",
    "ERROR_REASONS",
    "MESSAGE_LIMIT",
    "RETRYABLE_ERROR_CODES",
    "RETRYABLE_STRING_CODES",
    "SHIFT_CLOSED_CODES",
    "BillzAuthError",
    "classify",
    "error_details",
    "int_or_none",
    "json_body",
]
