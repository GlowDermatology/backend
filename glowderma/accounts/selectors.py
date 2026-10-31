"""Read side of `accounts`: the staff directory and the staff accounts (contract §2.16 ``GET staff/``), and
the customer sessions (B-W1: customer contract §2.2.10, admin §2.6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypedDict

from django.core.exceptions import ValidationError
from django.db.models import (
    Case,
    CharField,
    Count,
    Exists,
    F,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Lower, NullIf, TruncDay
from django.db.models.lookups import Contains

from glowderma.accounts.models import (
    AuthSession,
    Customer,
    CustomerSocialIdentity,
    SmsCode,
    SocialIdentity,
    StaffUser,
)
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.search import SearchFold
from glowderma.core.timeutils import TASHKENT

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


# --- ops console customers dashboard (glowderma.reporting.dashboards) ---------------------------------------

PHONE_ATTACHED_ACTION = "customer.phone_attached"
# A social sign-up creates the customer and its identity in one transaction: an identity linked within this
# window of ``registered_at`` is the sign-up method; a later one is a link from the settings screen.
SOCIAL_SIGNUP_WINDOW = timedelta(minutes=2)
SIGNUP_PHONE, SIGNUP_EMAIL = "phone", "email"
OTP_SMS, OTP_EMAIL = "sms", "email"
NO_DEVICE = "unknown"


def _phone_attached() -> Exists:
    return Exists(
        AuditEntry.objects.filter(
            action=PHONE_ATTACHED_ACTION,
            entity_type="customer",
            entity_id=Cast(OuterRef("pk"), output_field=CharField()),
        )
    )


def signups_by_day_and_method(start: datetime, end: datetime) -> dict[tuple[date, str], int]:
    """``{(Tashkent day, method): customers}`` registered in ``[start, end)`` (deleted ones included).

    The account stores no sign-up method, so it is derived: a social identity linked with the account
    (:data:`SOCIAL_SIGNUP_WINDOW`) names its provider; else an account that has a phone it did not attach
    later (no ``customer.phone_attached`` audit row) signed up by phone; else by e-mail. One query."""
    social = (
        CustomerSocialIdentity.objects.filter(customer_id=OuterRef("pk"), created_at__lte=OuterRef("cutoff"))
        .order_by("created_at", "id")
        .values("provider")[:1]
    )
    rows = (
        Customer.objects.filter(registered_at__gte=start, registered_at__lt=end)
        .annotate(cutoff=F("registered_at") + SOCIAL_SIGNUP_WINDOW)
        .annotate(social=Subquery(social), attached=_phone_attached())
        .annotate(
            method=Case(
                When(social__isnull=False, then=F("social")),
                When(phone__isnull=False, attached=False, then=Value(SIGNUP_PHONE)),
                default=Value(SIGNUP_EMAIL),
                output_field=CharField(),
            ),
            day=TruncDay("registered_at", tzinfo=TASHKENT),
        )
        .order_by()
        .values("day", "method")
        .annotate(n=Count("id"))
        .values_list("day", "method", "n")
    )
    return {(day.astimezone(TASHKENT).date(), str(method)): int(n) for day, method, n in rows}


@dataclass(frozen=True, slots=True)
class PhoneCoverage:
    phoneless: int  # active accounts without a phone (checkout, booking and the QR ask for one first)
    attached_later: int  # accounts (any status) that attached their phone after signing up without one
    active: int


def phone_coverage() -> PhoneCoverage:
    """Hybrid-identity phone coverage (one aggregate over the customers)."""
    row = Customer.objects.aggregate(
        phoneless=Count("id", filter=Q(phone__isnull=True, status=enums.CustomerStatus.ACTIVE)),
        attached=Count("id", filter=Q(_phone_attached())),
        active=Count("id", filter=Q(status=enums.CustomerStatus.ACTIVE)),
    )
    return PhoneCoverage(
        phoneless=int(row["phoneless"] or 0),
        attached_later=int(row["attached"] or 0),
        active=int(row["active"] or 0),
    )


def open_customer_sessions_by_platform(now: datetime) -> dict[str, int]:
    """``{platform: sessions}`` of the open customer sessions (not revoked, before their absolute expiry), by
    their device's platform (:data:`NO_DEVICE` without one). One query."""
    rows = (
        AuthSession.objects.filter(audience="customer", revoked_at__isnull=True, absolute_expires_at__gt=now)
        .order_by()
        .values("device__platform")
        .annotate(n=Count("id"))
        .values_list("device__platform", "n")
    )
    return {str(platform or NO_DEVICE): int(n) for platform, n in rows}


@dataclass(frozen=True, slots=True)
class OtpVolume:
    channel: str  # "sms" | "email"
    purpose: str  # SmsCode.Purpose value
    last_day: int
    last_week: int
    verified_week: int
    locked_day: int  # codes whose wrong attempts reached the limit (the account is locked out for a while)
    locked_week: int


def otp_volume(now: datetime) -> list[OtpVolume]:
    """One-time codes issued in the last 24 h / 7 days by channel and purpose, with the verified ones and the
    attempt-limit lockouts (``exhausted_at``). One query over the last week's ``SmsCode`` rows; the Redis
    day caps (per target, device, IP) and the hourly budget keep no rows, so their refusals are not here."""
    day, week = now - timedelta(days=1), now - timedelta(days=7)
    rows = (
        SmsCode.objects.filter(created_at__gte=week)
        .annotate(
            channel=Case(
                When(~Q(phone=""), then=Value(OTP_SMS)), default=Value(OTP_EMAIL), output_field=CharField()
            )
        )
        .order_by()
        .values("channel", "purpose")
        .annotate(
            last_day=Count("id", filter=Q(created_at__gte=day)),
            last_week=Count("id"),
            verified_week=Count("id", filter=Q(consumed_at__isnull=False)),
            locked_day=Count("id", filter=Q(exhausted_at__gte=day)),
            locked_week=Count("id", filter=Q(exhausted_at__isnull=False)),
        )
    )
    volume = [
        OtpVolume(
            channel=str(row["channel"]),
            purpose=str(row["purpose"]),
            last_day=int(row["last_day"]),
            last_week=int(row["last_week"]),
            verified_week=int(row["verified_week"]),
            locked_day=int(row["locked_day"]),
            locked_week=int(row["locked_week"]),
        )
        for row in rows
    ]
    return sorted(volume, key=lambda row: (row.channel, -row.last_week, row.purpose))
