"""Read side of `accounts`: the staff directory and the staff accounts (contract §2.16 ``GET staff/``), and
the customer sessions (B-W1: customer contract §2.2.10, admin §2.6)."""

from __future__ import annotations

from typing import TypedDict

from django.core.exceptions import ValidationError
from django.db.models import F, OuterRef, Prefetch, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce, Lower, NullIf
from django.db.models.lookups import Contains

from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.core import timeutils
from glowderma.core.search import SearchFold

# Only the directory columns: never the password hash (or last_login, is_superuser) per row.
DIRECTORY_COLUMNS = ("id", "full_name", "email", "is_active")
# The manager's rows add the role and the creation time (``created_at`` = ``date_joined``).
MEMBER_COLUMNS = (*DIRECTORY_COLUMNS, "is_manager", "date_joined")


class StaffFilters(TypedDict, total=False):
    search: str
    is_active: bool


def _last_sign_in() -> Subquery:
    """The newest session's ``created_at``: every sign-in (password or social) opens one session, and staff
    sessions are never purged, so it is the last sign-in (``None``: never signed in)."""
    newest = AuthSession.objects.filter(staff_id=OuterRef("pk")).order_by("-created_at").values("created_at")
    return Subquery(newest[:1])


def with_member_columns(rows: QuerySet[StaffUser]) -> QuerySet[StaffUser]:
    """``rows`` with what a ``StaffMember`` renders: the role and creation time, ``last_login_at`` (one
    correlated subquery) and the linked providers (one prefetch query for the whole page, by provider)."""
    identities = SocialIdentity.objects.only("id", "staff_id", "provider").order_by("provider")
    return (
        rows.only(*MEMBER_COLUMNS)
        .annotate(last_login_at=_last_sign_in())
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


def customer_session_open(session_id: str) -> bool:
    """Whether the customer session ``session_id`` is open (not revoked, not past its absolute expiry): a
    loyalty QR token is honoured only while the session that issued it is (B-W1)."""
    try:
        return AuthSession.objects.filter(
            pk=session_id,
            audience="customer",
            revoked_at__isnull=True,
            absolute_expires_at__gt=timeutils.now(),
        ).exists()
    except ValidationError:  # not a UUID
        return False
