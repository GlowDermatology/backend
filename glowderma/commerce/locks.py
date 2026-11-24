"""Session-level advisory locks that serialise one order's calls to a provider: a carrier (Yandex Delivery
plan §3.6, §3.8, decision 9) and Uzum's payment form registration (``uzum:pay:{order_id}``).

Every flow that calls Yandex for an order — the dispatch handler (``commerce.tasks.dispatch_courier``) and
the carrier-side cancel (``commerce.services.cancel_at_carrier``) — holds ``yandex:{order_id}`` across its
calls, so two of them never talk to Yandex for the same order at once (two dispatch deliveries, or a cancel
racing an ``accept``). The lock holds no row and no open transaction: the order row is locked only for the
short planning and recording transactions around the calls. A busy lock raises :class:`YandexBusy` at once
(the dispatch handler defers, an admin request answers 409).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection


class YandexBusy(RuntimeError):
    """Another worker or request is talking to Yandex for this order right now."""


def yandex_lock_key(order_id: int) -> str:
    return f"yandex:{order_id}"


@contextmanager
def yandex_lock(order_id: int) -> Iterator[None]:
    """Hold ``pg_try_advisory_lock(hashtext('yandex:{order_id}'))`` for the block (session level: it
    survives the block's own commits); :class:`YandexBusy` when another session holds it."""
    key = yandex_lock_key(order_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [key])
        row = cursor.fetchone()
    if not row or not row[0]:
        raise YandexBusy(f"order {order_id}: another worker is talking to Yandex for it")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", [key])


class UzumBusy(RuntimeError):
    """Another request is registering an Uzum form for this order right now."""


def uzum_pay_lock_key(order_id: int) -> str:
    return f"uzum:pay:{order_id}"


@contextmanager
def uzum_pay_lock(order_id: int) -> Iterator[None]:
    """Hold ``pg_try_advisory_lock(hashtext('uzum:pay:{order_id}'))`` while an Uzum form is registered for
    the order (no row lock, no open transaction: ``commerce.uzum_flow.prepare_form``); :class:`UzumBusy`
    when another session holds it."""
    key = uzum_pay_lock_key(order_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [key])
        row = cursor.fetchone()
    if not row or not row[0]:
        raise UzumBusy(f"order {order_id}: another request is registering an Uzum form")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", [key])


__all__ = ["UzumBusy", "YandexBusy", "uzum_pay_lock", "uzum_pay_lock_key", "yandex_lock", "yandex_lock_key"]
