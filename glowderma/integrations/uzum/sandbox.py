"""Uzum callback bodies as Uzum's terminal would post them (the offline sandbox suite and
``dev/…/simulate/``). Pure helpers; the callback carries no signature, so nothing is signed."""

from __future__ import annotations

from typing import Any


def callback(
    *,
    order_id: str,
    order_number: str,
    operation_type: str = "COMPLETE",
    operation_state: str = "SUCCESS",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "orderId": order_id,
        "orderNumber": order_number,
        "operationType": operation_type,
        "operationState": operation_state,
    }
    body.update(extra)
    return body
