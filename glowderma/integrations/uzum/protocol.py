"""Pure helpers of the Uzum Checkout protocol (docs/integrations/uzum.md): amounts, locales, order
numbers, status values, error codes and the parsing of ``getOrderStatus`` / callbacks."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .ports import Operation, OrderStatus

PATH_REGISTER = "/api/v1/payment/register"
PATH_ORDER_STATUS = "/api/v1/payment/getOrderStatus"
PATH_REFUND = "/api/v1/acquiring/refund"
CURRENCY_UZS = 860
PAY_TYPE_ONE_STEP = "ONE_STEP"
VIEW_TYPE_REDIRECT = "REDIRECT"
OK = 0

# Order statuses (AcquiringStatus).
REGISTERED = "REGISTERED"
AUTHORIZED = "AUTHORIZED"
COMPLETED = "COMPLETED"
DECLINED = "DECLINED"
REFUNDED = "REFUNDED"
REVERSED = "REVERSED"
ORDER_STATUSES = frozenset(
    {REGISTERED, AUTHORIZED, COMPLETED, DECLINED, REFUNDED, REVERSED, "TOP_UP_COMPLETED"}
)
# Operation types and states.
OP_REFUND = "REFUND"
OP_SUCCESS = "SUCCESS"
OP_FAIL = "FAIL"
OP_IN_PROGRESS = "IN_PROGRESS"

# errorCode values the flows tell apart (spec "Error codes").
ERR_ORDER_NUMBER_TAKEN = 3027
ERR_OPERATION_EXISTS = 3028
ERR_REFUND_AMOUNT = 3021
ERR_REFUND_TIMEOUT = 3022
ERR_REFUND_CART_MISSING = 3045
ERR_REFUND_CART_INVALID = 3046
ERR_CART_TAX_DATA = 3055
ERR_CART_TOTAL = 3059
ERR_SESSION_TIMEOUT = 3041
ERR_FORM_CLOSED = 3042

# Content-Language takes exactly these (LocaleType); note "en-EN", not "en".
LOCALES = {"uz": "uz-UZ", "ru": "ru-RU", "en": "en-EN"}
ORDER_NUMBER_MAX = 36
_ORDER_NUMBER = re.compile(r"(?P<prefix>[a-z0-9]{1,8})-(?P<payment>[1-9][0-9]{0,17})-(?P<nonce>[0-9a-f]{8})")


def to_tiyin(som: int) -> int:
    """So'm (our ``Payment.amount``) → tiyin (Uzum's amounts)."""
    if isinstance(som, bool) or not isinstance(som, int) or som <= 0:
        raise ValueError("an amount is a positive whole number of so'm")
    return som * 100


def matches_som(tiyin: object, som: int) -> bool:
    """Whether Uzum's tiyin amount is exactly ``som`` so'm (never rounded: 1 tiyin off is a mismatch)."""
    return isinstance(tiyin, int) and not isinstance(tiyin, bool) and tiyin == som * 100


def content_language(language: str) -> str:
    return LOCALES.get(language, LOCALES["uz"])


def new_order_number(prefix: str, payment_id: int) -> str:
    """``<prefix>-<payment id>-<8 hex>``: a fresh number per register call (Uzum refuses a number twice with
    3027, and a register whose answer was lost may still have taken it)."""
    number = f"{prefix}-{payment_id}-{secrets.token_hex(4)}"
    if len(number) > ORDER_NUMBER_MAX:
        raise ValueError("order number longer than 36 characters")
    return number


def payment_id_of(order_number: object, prefix: str) -> int | None:
    """Our payment id in one of our order numbers of this environment's ``prefix``, else ``None``."""
    if not isinstance(order_number, str):
        return None
    match = _ORDER_NUMBER.fullmatch(order_number)
    if match is None or match["prefix"] != prefix:
        return None
    return int(match["payment"])


def operation_id(refund_id: int) -> str:
    """Our ``X-Operation-Id`` of a refund: the same for every submission of that refund row, so a resend
    after a lost answer is deduplicated by Uzum (3028) instead of refunding twice."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"glowderma:uzum:refund:{refund_id}"))


def is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def redirect_url_ok(url: object, hosts: Collection[str]) -> bool:
    """An https URL (on one of ``hosts`` when that list is set): the only page we send a payer to."""
    if not isinstance(url, str) or len(url) > 2083:
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    return not hosts or parts.hostname.lower() in hosts


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object, limit: int = 64) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_order_status(result: Any) -> OrderStatus | None:
    """``getOrderStatus.result`` → :class:`OrderStatus`; ``None`` when a required field is missing or of the
    wrong type (the caller treats that as "status unknown", never as a state)."""
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    amounts = [_int(result.get(name, 0)) for name in ("completedAmount", "refundedAmount", "reversedAmount")]
    amount = _int(result.get("amount"))
    if status not in ORDER_STATUSES or amount is None or any(value is None for value in amounts):
        return None
    if not is_uuid(result.get("orderId")):
        return None
    operations: list[Operation] = []
    raw_operations = result.get("operations") or []
    if not isinstance(raw_operations, list):
        return None
    for raw in raw_operations:
        if not isinstance(raw, dict):
            return None
        operations.append(
            Operation(
                operation_id=_text(raw.get("operationId")),
                operation_type=_text(raw.get("operationType"), 32),
                state=_text(raw.get("state"), 32),
                merchant_operation_id=_text(raw.get("merchantOperationId")).lower(),
                done_at=_instant(raw.get("doneAt")),
            )
        )
    completed, refunded, reversed_ = (int(value or 0) for value in amounts)
    return OrderStatus(
        order_id=str(result["orderId"]).lower(),
        status=str(status),
        merchant_order_id=_text(result.get("merchantOrderId")),
        amount=amount,
        completed_amount=completed,
        refunded_amount=refunded,
        reversed_amount=reversed_,
        operations=tuple(operations),
    )


CALLBACK_FIELDS = ("orderId", "orderNumber", "operationType", "operationState", "merchantOperationId")


def callback_fields(payload: Any) -> dict[str, str]:
    """The allow-listed fields of a callback body, as bounded text (what the inbox stores; never ``rrn``,
    card data or anything else the body carries)."""
    if not isinstance(payload, dict):
        return {}
    return {name: _text(payload.get(name)) for name in CALLBACK_FIELDS if isinstance(payload.get(name), str)}
