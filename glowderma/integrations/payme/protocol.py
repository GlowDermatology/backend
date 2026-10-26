"""Payme Merchant API (JSON-RPC 2.0) protocol helpers (customer contract §2.13.2) — pure, no database.

Amounts are tiyin (so'm × 100) and exist only here and in the Payme views; times are epoch milliseconds. The
account-error codes and ``-32400`` are protocol constants (verify M3 against the ``test.paycom.uz`` checklist).
"""

from __future__ import annotations

import base64
import binascii
import hmac
from typing import Any, Final, TypeIs
from urllib.parse import quote

ERR_TRANSPORT: Final = -32300  # non-POST
ERR_PARSE: Final = -32700
ERR_INVALID_REQUEST: Final = -32600
ERR_METHOD_NOT_FOUND: Final = -32601
ERR_INSUFFICIENT_PRIVILEGE: Final = -32504
ERR_SYSTEM: Final = -32400
ERR_WRONG_AMOUNT: Final = -31001
ERR_TRANSACTION_NOT_FOUND: Final = -31003
ERR_CANNOT_CANCEL: Final = -31007
ERR_CANNOT_PERFORM: Final = -31008
ERR_ORDER_NOT_FOUND: Final = -31050
ERR_ORDER_BUSY: Final = -31051

MESSAGES: Final[dict[int, dict[str, str]]] = {
    ERR_TRANSPORT: {
        "uz": "So‘rov POST bo‘lishi kerak.",
        "ru": "Метод запроса должен быть POST.",
        "en": "Request method must be POST.",
    },
    ERR_PARSE: {"uz": "JSON xato.", "ru": "Ошибка парсинга JSON.", "en": "Parse error."},
    ERR_INVALID_REQUEST: {"uz": "So‘rov noto‘g‘ri.", "ru": "Неверный запрос.", "en": "Invalid request."},
    ERR_METHOD_NOT_FOUND: {"uz": "Metod topilmadi.", "ru": "Метод не найден.", "en": "Method not found."},
    ERR_INSUFFICIENT_PRIVILEGE: {
        "uz": "Ruxsat yetarli emas.",
        "ru": "Недостаточно привилегий для выполнения метода.",
        "en": "Insufficient privileges.",
    },
    ERR_SYSTEM: {"uz": "Tizim xatosi.", "ru": "Системная ошибка.", "en": "System error."},
    ERR_WRONG_AMOUNT: {"uz": "Noto‘g‘ri summa.", "ru": "Неверная сумма.", "en": "Wrong amount."},
    ERR_TRANSACTION_NOT_FOUND: {
        "uz": "Tranzaksiya topilmadi.",
        "ru": "Транзакция не найдена.",
        "en": "Transaction not found.",
    },
    ERR_CANNOT_CANCEL: {
        "uz": "Buyurtma bajarilgan, tranzaksiyani bekor qilib bo‘lmaydi.",
        "ru": "Заказ выполнен. Невозможно отменить транзакцию.",
        "en": "The order is fulfilled; the transaction cannot be cancelled.",
    },
    ERR_CANNOT_PERFORM: {
        "uz": "Amalni bajarib bo‘lmaydi.",
        "ru": "Невозможно выполнить операцию.",
        "en": "Unable to perform the operation.",
    },
    ERR_ORDER_NOT_FOUND: {"uz": "Buyurtma topilmadi.", "ru": "Заказ не найден.", "en": "Order not found."},
    ERR_ORDER_BUSY: {
        "uz": "Buyurtma boshqa to‘lov bilan kutilmoqda.",
        "ru": "Заказ ожидает оплаты другой транзакцией.",
        "en": "The order is awaiting another transaction.",
    },
}
# A state-1 transaction older than this is cancelled by us on the next touch (reason 4, answer -31008).
TIMEOUT_MS: Final = 43_200_000
REASON_TIMEOUT: Final = 4
STATE_CREATED: Final = 1
STATE_PERFORMED: Final = 2
STATE_CANCELLED: Final = -1
STATE_CANCELLED_AFTER_PERFORM: Final = -2
# What an unexpected exception answers: HTTP 200 with JSON-RPC -32400, everything rolled back (verify M3).
INTERNAL_ERROR_RESPONSE: Final = "rpc_-32400"
METHODS: Final = frozenset(
    {
        "CheckPerformTransaction",
        "CreateTransaction",
        "PerformTransaction",
        "CancelTransaction",
        "CheckTransaction",
        "GetStatement",
    }
)
READ_ONLY_METHODS: Final = frozenset({"CheckPerformTransaction", "CheckTransaction", "GetStatement"})
PAYME_LOGIN: Final = "Paycom"


def auth_ok(header: str, key: str) -> bool:
    """``Authorization: Basic base64("Paycom:" + key)`` compared with ``compare_digest``."""
    if not key or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic ") :].strip(), validate=True).decode()
    except binascii.Error, UnicodeDecodeError:
        return False
    return hmac.compare_digest(decoded.encode(), f"{PAYME_LOGIN}:{key}".encode())


def basic_auth(key: str) -> str:
    """The header Payme sends (sandbox helper and tests)."""
    return "Basic " + base64.b64encode(f"{PAYME_LOGIN}:{key}".encode()).decode()


def rpc_error(request_id: Any, code: int, data: Any = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": MESSAGES[code], "data": data},
    }


def rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def is_int(value: object) -> TypeIs[int]:
    """A JSON integer (``bool`` excluded; ``78440000.5`` and ``"78440000"`` are not)."""
    return isinstance(value, int) and not isinstance(value, bool)


def checkout_url(
    base: str,
    *,
    merchant_id: str,
    account_field: str,
    account_ref: str,
    amount_tiyin: int,
    lang: str,
    return_url: str,
) -> str:
    """``{PAYME_CHECKOUT_URL}/{base64("m=…;ac.{field}=…;a=…;l=…;c=…;ct=15000")}`` (§2.7.2, verify M3)."""
    raw = (
        f"m={merchant_id};ac.{account_field}={account_ref};a={amount_tiyin};l={lang};"
        f"c={quote(return_url, safe='')};ct=15000"
    )
    return f"{base.rstrip('/')}/{base64.b64encode(raw.encode()).decode()}"
