"""Correctly signed Click SHOP API requests (plan §5.1): the offline sandbox suite and ``dev/…/simulate/``.

Pure helpers: they build the form parameters a Click server would post, signed with ``CLICK_SECRET_KEY``.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from . import protocol

DEFAULT_SIGN_TIME = "2026-10-23 14:03:00"


def prepare_params(
    *,
    click_trans_id: int | str,
    merchant_trans_id: str,
    amount: int | str,
    click_paydoc_id: int | str = 0,
    error: int | str = 0,
    sign_time: str = DEFAULT_SIGN_TIME,
    service_id: str | None = None,
    action: str = protocol.ACTION_PREPARE,
    secret: str | None = None,
    **overrides: Any,
) -> dict[str, str]:
    params = {
        "click_trans_id": str(click_trans_id),
        "service_id": str(service_id if service_id is not None else settings.CLICK_SERVICE_ID),
        "click_paydoc_id": str(click_paydoc_id or click_trans_id),
        "merchant_trans_id": merchant_trans_id,
        "amount": str(amount),
        "action": action,
        "error": str(error),
        "error_note": "Success" if str(error) == "0" else "Error",
        "sign_time": sign_time,
    }
    params.update({key: str(value) for key, value in overrides.items()})
    params["sign_string"] = protocol.signature(
        params, settings.CLICK_SECRET_KEY if secret is None else secret, complete=False
    )
    return params


def complete_params(
    *,
    click_trans_id: int | str,
    merchant_trans_id: str,
    merchant_prepare_id: int | str,
    amount: int | str,
    click_paydoc_id: int | str = 0,
    error: int | str = 0,
    sign_time: str = DEFAULT_SIGN_TIME,
    service_id: str | None = None,
    action: str = protocol.ACTION_COMPLETE,
    secret: str | None = None,
    **overrides: Any,
) -> dict[str, str]:
    params = {
        "click_trans_id": str(click_trans_id),
        "service_id": str(service_id if service_id is not None else settings.CLICK_SERVICE_ID),
        "click_paydoc_id": str(click_paydoc_id or click_trans_id),
        "merchant_trans_id": merchant_trans_id,
        "merchant_prepare_id": str(merchant_prepare_id),
        "amount": str(amount),
        "action": action,
        "error": str(error),
        "error_note": "Success" if str(error) == "0" else "Error",
        "sign_time": sign_time,
    }
    params.update({key: str(value) for key, value in overrides.items()})
    params["sign_string"] = protocol.signature(
        params, settings.CLICK_SECRET_KEY if secret is None else secret, complete=True
    )
    return params
