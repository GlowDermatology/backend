"""The campaign approval state machine as data (admin contract §2.8; plan "campaign approval" §3).

``notifications.services`` consults :data:`TRANSITIONS` before every state change; nothing here touches the
database. Each :class:`Transition` is one row of the plan's table: who may drive it, the states it starts
from, the state it ends in and the audit action it records. ``idempotent_in`` names the states in which a
repeat of the same request answers 200 with the row unchanged (no second audit row); any other wrong state
is 409 ``invalid_transition``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from glowderma.core.api.exceptions import InvalidTransition
from glowderma.notifications.models import Campaign

_S = Campaign.State


class Actor(StrEnum):
    """Who drives a transition (the plan's "who" column)."""

    STAFF = "staff"  # any active staff member
    AUTHOR_OR_SUPERUSER = "author_or_superuser"  # the campaign's author, or any superuser (else 403)
    SUPERUSER = "superuser"  # ``StaffUser.is_superuser`` (``accounts.permissions.IsSuperuser``)
    SYSTEM = "system"  # the beat, the outbox handler or the dead-letter hook (audit actor "system")


@dataclass(frozen=True, slots=True)
class Transition:
    name: str
    sources: frozenset[str]  # the states the action is valid from (empty = creates the row)
    target: str | None  # None = the row is deleted, or the target depends on the outcome
    actor: Actor
    audit_action: str
    message: str  # the 409 ``invalid_transition`` text for a wrong source state
    idempotent_in: frozenset[str] = frozenset()  # repeat → 200 unchanged, no audit


MESSAGE_NOT_EDITABLE = "Faqat qoralamani o‘zgartirish mumkin."
MESSAGE_NOT_DRAFT = "Xabar qoralama holatida emas."
MESSAGE_NOT_TESTED = "Avval test yuboring va qurilmada ko‘rganingizni tasdiqlang."
MESSAGE_NOT_APPROVABLE = "Faqat test tasdiqlangan xabarni yuborishga tasdiqlash mumkin."
MESSAGE_NOT_RETURNABLE = "Faqat rejalashtirilgan yoki bekor qilingan xabarni qoralamaga qaytarish mumkin."
MESSAGE_CANCEL_DRAFT = "Qoralamani bekor qilish shart emas: uni o‘chiring yoki o‘zgartiring."
MESSAGE_CANCEL_STARTED = "Yuborish boshlangan: xabarni bekor qilib bo‘lmaydi."
MESSAGE_NOT_SENDING = "Xabar hozir yuborilmayapti: to‘xtatadigan narsa yo‘q."
MESSAGE_NOT_DUE = "Xabar hali yuborishga tayyor emas."

TRANSITIONS: dict[str, Transition] = {
    transition.name: transition
    for transition in (
        Transition("create", frozenset(), _S.DRAFT, Actor.STAFF, "notification.created", MESSAGE_NOT_DRAFT),
        Transition(
            "update",
            frozenset({_S.DRAFT, _S.TESTED}),
            _S.DRAFT,  # a content change; a schedule-only edit keeps ``tested``
            Actor.AUTHOR_OR_SUPERUSER,
            "notification.draft_updated",
            MESSAGE_NOT_EDITABLE,
        ),
        Transition(
            "delete",
            frozenset({_S.DRAFT, _S.TESTED}),
            None,
            Actor.AUTHOR_OR_SUPERUSER,
            "notification.draft_deleted",
            MESSAGE_NOT_EDITABLE,
        ),
        Transition(
            "test",
            frozenset({_S.DRAFT, _S.TESTED}),
            None,  # the state does not change
            Actor.SUPERUSER,
            "notification.test_sent",
            MESSAGE_NOT_EDITABLE,
        ),
        Transition(
            "confirm",
            frozenset({_S.DRAFT, _S.TESTED}),
            _S.TESTED,
            Actor.SUPERUSER,
            "notification.test_confirmed",
            MESSAGE_NOT_EDITABLE,
        ),
        Transition(
            "approve",
            frozenset({_S.TESTED}),
            _S.SCHEDULED,
            Actor.SUPERUSER,
            "notification.approved",
            MESSAGE_NOT_APPROVABLE,  # a ``draft`` gets MESSAGE_NOT_TESTED from the service
            idempotent_in=frozenset({_S.SCHEDULED}),
        ),
        Transition(
            "return_to_draft",
            frozenset({_S.SCHEDULED, _S.CANCELLED}),
            _S.DRAFT,
            Actor.SUPERUSER,
            "notification.returned_to_draft",
            MESSAGE_NOT_RETURNABLE,
            idempotent_in=frozenset({_S.DRAFT}),
        ),
        Transition(
            "cancel",
            frozenset({_S.SCHEDULED}),
            _S.CANCELLED,
            Actor.SUPERUSER,
            "notification.cancelled",
            MESSAGE_CANCEL_DRAFT,
            idempotent_in=frozenset({_S.CANCELLED}),
        ),
        Transition(
            "start",
            frozenset({_S.SCHEDULED}),
            _S.SENDING,  # or ``sent`` at once when nobody can be reached
            Actor.SYSTEM,
            "notification.send_started",
            MESSAGE_NOT_DUE,
        ),
        Transition(
            "stop",
            frozenset({_S.SENDING}),
            _S.STOPPING,
            Actor.SUPERUSER,
            "notification.stop_requested",
            MESSAGE_NOT_SENDING,
            idempotent_in=frozenset({_S.STOPPING}),
        ),
        Transition(
            "stopped",
            frozenset({_S.STOPPING}),
            None,  # ``partially_sent`` when a push went out, else ``cancelled``
            Actor.SYSTEM,
            "notification.stopped",
            MESSAGE_NOT_SENDING,
        ),
        Transition(
            "failed",
            frozenset({_S.SENDING, _S.STOPPING}),
            None,  # ``partially_sent`` when a push went out, else ``failed``
            Actor.SYSTEM,
            "notification.failed",
            MESSAGE_NOT_SENDING,
        ),
    )
}

# Audit actions of the test-device registry (no campaign state involved).
AUDIT_TEST_DEVICE_REGISTERED = "test_device.registered"
AUDIT_TEST_DEVICE_REMOVED = "test_device.removed"


def transition(name: str) -> Transition:
    return TRANSITIONS[name]


def check(name: str, state: str) -> bool:
    """``True`` when ``state`` is a source of the transition; ``False`` when a repeat should answer unchanged;
    409 ``invalid_transition`` with the transition's message otherwise."""
    row = TRANSITIONS[name]
    if state in row.sources:
        return True
    if state in row.idempotent_in:
        return False
    raise InvalidTransition(row.message)
