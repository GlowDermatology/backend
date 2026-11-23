"""The staff directory and the staff accounts (admin contract §2.16 ``GET staff/``): the columns a row
may carry (never the password hash), the newest password link of each member and the list filters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from django.db.models import (
    Exists,
    F,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, Lower, NullIf
from django.db.models.lookups import Contains
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts import second_factor
from glowderma.accounts.models import (
    AuthSession,
    SocialIdentity,
    StaffPasswordToken,
    StaffUser,
)
from glowderma.core import enums, timeutils
from glowderma.core.search import SearchFold

# Only the directory columns: never the password hash (or last_login, is_superuser) per row.
DIRECTORY_COLUMNS = ("id", "full_name", "email", "is_active")
# The manager's rows add the role (manager flag, Staff/Specialist, superuser, ops console), the creation time
# (``created_at`` = ``date_joined``) and the password state (feat/admin-ux).
MEMBER_COLUMNS = (
    *DIRECTORY_COLUMNS,
    "is_manager",
    "role",
    "is_superuser",
    "is_staff",
    "date_joined",
    "must_change_password",
    "password_changed_at",
)
# The newest password link's columns, annotated as ``invite_<name>`` (:func:`invite_of`).
INVITE_COLUMNS = ("created_at", "sent_at", "expires_at", "used_at", "revoked_at")


class StaffFilters(TypedDict, total=False):
    search: str
    is_active: bool


def _last_sign_in() -> Subquery:
    """The newest session's ``created_at``: every sign-in (password or social) opens one session, and staff
    sessions are never purged, so it is the last sign-in (``None``: never signed in)."""
    newest = AuthSession.objects.filter(staff_id=OuterRef("pk")).order_by("-created_at").values("created_at")
    return Subquery(newest[:1])


def _newest_link(column: str) -> Subquery:
    newest = StaffPasswordToken.objects.filter(staff_id=OuterRef("pk")).order_by("-created_at", "-id")
    return Subquery(newest.values(column)[:1])


@dataclass(frozen=True, slots=True)
class Invite:
    """``StaffMember.invite``: the newest password link's state (``StaffInviteState``) and times."""

    state: str
    sent_at: datetime | None
    expires_at: datetime | None


def invite_of(user: StaffUser) -> Invite:
    """The ``invite`` of a row read through :func:`with_member_columns`: ``none`` without any link, else the
    newest link's ``used`` / ``revoked`` / ``expired`` / ``pending`` (open: queued or sent)."""
    values = {name: getattr(user, f"invite_{name}", None) for name in INVITE_COLUMNS}
    if values["created_at"] is None:
        return Invite(enums.StaffInviteState.NONE, None, None)
    if values["used_at"] is not None:
        state = enums.StaffInviteState.USED
    elif values["revoked_at"] is not None:
        state = enums.StaffInviteState.REVOKED
    elif values["expires_at"] is not None and values["expires_at"] <= timeutils.now():
        state = enums.StaffInviteState.EXPIRED
    else:
        state = enums.StaffInviteState.PENDING
    return Invite(state, values["sent_at"], values["expires_at"])


def _panel_mfa() -> Exists:
    """A confirmed admin panel TOTP device (feat/admin-ux; the ops console's devices do not count)."""
    return Exists(
        TOTPDevice.objects.filter(
            user_id=OuterRef("pk"), confirmed=True, name=second_factor.PANEL_DEVICE_NAME
        )
    )


def with_member_columns(rows: QuerySet[StaffUser]) -> QuerySet[StaffUser]:
    """``rows`` with what a ``StaffMember`` renders: the role, creation time and password state,
    ``mfa_enabled`` (admin panel TOTP), ``last_login_at`` and the newest password link (correlated
    subqueries of the same query) and the linked providers (one prefetch query for the whole page, by
    provider)."""
    identities = SocialIdentity.objects.only("id", "staff_id", "provider").order_by("provider")
    return (
        rows.only(*MEMBER_COLUMNS)
        .annotate(
            last_login_at=_last_sign_in(),
            **{f"invite_{name}": _newest_link(name) for name in INVITE_COLUMNS},
            mfa_enabled=_panel_mfa(),
        )
        .prefetch_related(Prefetch("social_identities", queryset=identities))
    )


def staff_list(filters: StaffFilters, *, members: bool = False) -> QuerySet[StaffUser]:
    """Staff users by display name (full name, else email; case-insensitive), ties by ``id``.

    ``search`` matches the full name (folded, decision GC-1) or the email (case-insensitive substring);
    ``is_active`` filters when given (the API defaults it to ``true``). ``members`` adds the manager's columns
    (:func:`with_member_columns`); without it only the directory columns are read.
    """
    rows = StaffUser.objects.only(*DIRECTORY_COLUMNS)
    if "is_active" in filters:
        rows = rows.filter(is_active=filters["is_active"])
    term = filters.get("search")
    if term:
        # the full name is folded like every name search (apostrophe variants and ё, decision GC-1)
        rows = rows.filter(
            Q(Contains(SearchFold(F("full_name")), SearchFold(Value(term)))) | Q(email__icontains=term)
        )
    if members:
        rows = with_member_columns(rows)
    display = Coalesce(NullIf(F("full_name"), Value("")), F("email"))
    return rows.order_by(Lower(display), "id")


def staff_member(staff_id: int) -> StaffUser | None:
    """One staff account with the ``StaffMember`` columns, or ``None``."""
    return with_member_columns(StaffUser.objects.filter(pk=staff_id)).first()


__all__ = [
    "DIRECTORY_COLUMNS",
    "INVITE_COLUMNS",
    "MEMBER_COLUMNS",
    "Invite",
    "StaffFilters",
    "invite_of",
    "staff_list",
    "staff_member",
    "with_member_columns",
]
