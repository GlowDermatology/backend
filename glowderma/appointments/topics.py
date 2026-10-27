"""Outbox topic ``appointments`` produces (design 03 §5): ``push.appointment``, handled by ``notifications``.

Payload keys: ``appointment_id``, ``action`` (``confirmed`` | ``rejected`` | ``rescheduled`` | ``cancelled``),
``event_id``, ``customer_id``, ``reschedule_no`` (int for ``rescheduled``, else ``null``); every key is always
present. The dedupe key below is the idempotency contract between producer and handler (``enqueue`` is
``get_or_create``), and the handler's ``InboxNotification.dedupe_key`` mirrors it (:func:`inbox_key`).
"""

from __future__ import annotations

PUSH_APPOINTMENT = "push.appointment"
PAYLOAD_KEYS: tuple[str, ...] = ("appointment_id", "action", "event_id", "customer_id", "reschedule_no")
PUSH_ACTIONS: tuple[str, ...] = ("confirmed", "rejected", "rescheduled", "cancelled")


def _suffix(appointment_id: int, action: str, reschedule_no: int | None) -> str:
    if action == "rescheduled":
        if reschedule_no is None:
            raise ValueError("A rescheduled push needs its reschedule_no")
        return f"{appointment_id}:rescheduled:{reschedule_no}"
    return f"{appointment_id}:{action}"


def push_appointment_key(appointment_id: int, action: str, reschedule_no: int | None = None) -> str:
    """``push.appointment:{id}:{action}`` (``…:rescheduled:{n}``): one push per action."""
    return f"{PUSH_APPOINTMENT}:{_suffix(appointment_id, action, reschedule_no)}"


def inbox_key(appointment_id: int, action: str, reschedule_no: int | None = None) -> str:
    """``InboxNotification.dedupe_key`` the notifications handler claims: ``appointment:{id}:{action}``."""
    return f"appointment:{_suffix(appointment_id, action, reschedule_no)}"
