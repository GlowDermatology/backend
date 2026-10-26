"""Shared pieces of the operator staff commands (``deactivatestaff``, ``reactivatestaff``,
``resetstaffpassword``, ``setstaffmanager``; admin contract §2.16, decision GST-6).

An operator runs them on the server (for instance when nobody who may manage staff can sign in). Each one
goes through the same audited service as the admin API: the audit actor is the staff member named by
``--actor`` (who asked for it) or, without it, ``system``; the ``reason`` is always ``manage.py <command>``.
Unlike the API they may target the actor's own account; the last-active-manager rule still holds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.core.management.base import CommandError, CommandParser
from rest_framework.exceptions import APIException

from glowderma.accounts.models import StaffUser


def add_target_arguments(parser: CommandParser) -> None:
    parser.add_argument("email", help="The email of the staff account.")
    parser.add_argument(
        "--actor",
        default="",
        help="The email of the active staff member who asked for it (the audit actor); default: system.",
    )


def target_staff(email: str) -> StaffUser:
    user = StaffUser.objects.filter(email__iexact=email.strip()).first()
    if user is None:
        raise CommandError(f"No staff account with the email {email.strip().lower()!r}")
    return user


def operator_actor(email: str) -> StaffUser | None:
    if not email.strip():
        return None
    actor = StaffUser.objects.filter(email__iexact=email.strip(), is_active=True).first()
    if actor is None:
        raise CommandError(f"--actor: no active staff member with the email {email.strip().lower()!r}")
    return actor


def run(call: Callable[[], Any]) -> Any:
    """``call()`` with a refused service write (409 ``conflict``, a field error) as a ``CommandError``."""
    try:
        return call()
    except APIException as exc:
        raise CommandError(str(exc.detail)) from exc
