"""Service tests for staff social sign-in (contract 2.1): social_login, link_identity, unlink_identity."""

from typing import Any

import pytest
from django.db.models import QuerySet
from django.test import override_settings
from rest_framework.exceptions import NotFound

from glowderma.accounts import services
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.api.exceptions import AuthFailed, Conflict
from glowderma.core.models import AuditEntry
from glowderma.integrations.oauth.ports import SocialClaims


def password_sid(user: StaffUser) -> str:
    """A password session of ``user`` (admin contract §2.1: only such a session links an identity)."""
    session, _ = services.start_staff_session(user, device_label="pytest")
    return str(session.pk)


pytestmark = pytest.mark.django_db


def google(
    subject: str = "g-1", email: str = "ali@gmail.com", *, verified: bool = True, hd: str | None = None
) -> SocialClaims:
    return SocialClaims(provider="google", subject=subject, email=email, email_verified=verified, hd=hd)


def facebook(subject: str = "fb-1", email: str = "ali@gmail.com") -> SocialClaims:
    return SocialClaims(provider="facebook", subject=subject, email=email, email_verified=False, hd=None)


def link(staff: StaffUser, claims: SocialClaims) -> SocialIdentity:
    return SocialIdentity.objects.create(
        staff=staff, provider=claims.provider, subject=claims.subject, email_at_link=claims.email
    )


def login(claims: SocialClaims) -> tuple[AuthSession, str]:
    return services.social_login(claims.provider, claims, device_label="pytest")


def failures() -> list[Any]:
    return [entry.after for entry in AuditEntry.objects.filter(action="auth.login_failed")]


def test_linked_identity_logs_in_and_audits(staff: StaffUser) -> None:
    identity = link(staff, google())
    session, refresh = login(google())
    assert session.staff == staff
    assert session.audience == "staff"
    assert len(refresh) >= 43
    identity.refresh_from_db()
    assert identity.last_login_at is not None
    entry = AuditEntry.objects.get(action="auth.login_succeeded")
    assert entry.after == {"email": staff.email, "method": "google"}
    assert entry.actor_id == staff.pk
    assert not AuditEntry.objects.filter(action="auth.social_linked").exists()


def test_facebook_identity_logs_in_regardless_of_email(staff: StaffUser) -> None:
    link(staff, facebook(email="old@example.com"))
    session, _ = login(facebook(email="new@example.com"))
    assert session.staff == staff


def test_unknown_identity_is_uniform_failure_for_known_and_unknown_email(staff: StaffUser) -> None:
    for claims in (google(email=staff.email), google(email="nobody@gmail.com")):
        with pytest.raises(AuthFailed) as info:
            login(claims)
        assert info.value.get_codes() == "social_account_not_allowed"
    assert failures() == [
        {"email": staff.email, "method": "google"},
        {"email": "nobody@gmail.com", "method": "google"},
    ]
    assert AuthSession.objects.count() == 0
    assert SocialIdentity.objects.count() == 0


def test_inactive_staff_identity_is_refused(staff: StaffUser) -> None:
    link(staff, google())
    staff.is_active = False
    staff.save(update_fields=["is_active"])
    with pytest.raises(AuthFailed) as info:
        login(google())
    assert info.value.get_codes() == "social_account_not_allowed"
    assert AuthSession.objects.count() == 0


def test_autolink_is_off_by_default(staff: StaffUser) -> None:
    with pytest.raises(AuthFailed):
        login(google(email=staff.email))
    assert SocialIdentity.objects.count() == 0


class TestAutolink:
    @pytest.fixture(autouse=True)
    def autolink_on(self, settings: Any) -> None:
        settings.STAFF_GOOGLE_AUTOLINK = True

    def test_gmail_links_on_first_use_and_audits(self) -> None:
        staff = StaffUserFactory.create(email="ali@gmail.com")
        session, _ = login(google(email="Ali@Gmail.com"))
        assert session.staff == staff
        identity = SocialIdentity.objects.get()
        assert (identity.staff, identity.provider, identity.subject) == (staff, "google", "g-1")
        assert identity.email_at_link == "Ali@Gmail.com"
        assert identity.last_login_at is not None
        linked = AuditEntry.objects.get(action="auth.social_linked")
        assert linked.after == {
            "provider": "google",
            "subject": "g-1",
            "email": "Ali@Gmail.com",
            "auto": True,
        }
        assert linked.actor_id == staff.pk
        assert AuditEntry.objects.filter(action="auth.login_succeeded").exists()
        # second sign-in matches the stored identity, no second link
        login(google(email="ali@gmail.com"))
        assert SocialIdentity.objects.count() == 1
        assert AuditEntry.objects.filter(action="auth.social_linked").count() == 1

    def test_workspace_account_links_when_hd_equals_email_domain(self) -> None:
        staff = StaffUserFactory.create(email="ali@glowderma.uz")
        session, _ = login(google(email="ali@glowderma.uz", hd="glowderma.uz"))
        assert session.staff == staff

    @override_settings(GOOGLE_ALLOWED_HD="glowderma.uz")
    def test_workspace_account_must_match_allowed_hd(self) -> None:
        StaffUserFactory.create(email="ali@other.uz")
        with pytest.raises(AuthFailed):
            login(google(email="ali@other.uz", hd="other.uz"))
        staff = StaffUserFactory.create(email="ali@glowderma.uz")
        session, _ = login(google(subject="g-2", email="ali@glowderma.uz", hd="glowderma.uz"))
        assert session.staff == staff

    @pytest.mark.parametrize(
        "claims",
        [
            google(email="ali@other.uz"),  # no hd: Google is not authoritative for a third-party domain
            google(email="ali@other.uz", hd="glowderma.uz"),  # hd differs from the email's domain
            google(email="ali@gmail.com", verified=False),
            google(email="ali@gmail.com", hd="gmail.com", verified=False),
            google(email="not-an-email"),
            facebook(email="ali@gmail.com"),
        ],
        ids=["other-domain", "hd-mismatch", "unverified-gmail", "unverified-hd", "no-at", "facebook"],
    )
    def test_refused_when_google_is_not_authoritative_or_provider_is_facebook(
        self, claims: SocialClaims
    ) -> None:
        StaffUserFactory.create(email=claims.email if "@" in claims.email else "x@gmail.com")
        with pytest.raises(AuthFailed) as info:
            login(claims)
        assert info.value.get_codes() == "social_account_not_allowed"
        assert SocialIdentity.objects.count() == 0

    def test_refused_for_inactive_staff_and_for_staff_already_linked_to_another_google_account(self) -> None:
        inactive = StaffUserFactory.create(email="off@gmail.com", is_active=False)
        with pytest.raises(AuthFailed):
            login(google(email=inactive.email))
        linked = StaffUserFactory.create(email="ali@gmail.com")
        link(linked, google(subject="g-existing", email=linked.email))
        with pytest.raises(AuthFailed):
            login(google(subject="g-new", email=linked.email))
        assert SocialIdentity.objects.count() == 1


def test_link_identity_creates_and_audits(staff: StaffUser) -> None:
    identity = services.link_identity(
        staff, "google", google(email="ali@glowderma.uz", hd="glowderma.uz"), session_id=password_sid(staff)
    )
    assert identity.email_at_link == "ali@glowderma.uz"
    assert identity.last_login_at is None
    entry = AuditEntry.objects.get(action="auth.social_linked")
    assert entry.after == {"provider": "google", "subject": "g-1", "email": "ali@glowderma.uz", "auto": False}
    assert entry.actor_id == staff.pk


def test_link_identity_conflicts(staff: StaffUser) -> None:
    other = StaffUserFactory.create()
    services.link_identity(staff, "google", google(subject="g-1"), session_id=password_sid(staff))
    with pytest.raises(Conflict) as info:
        services.link_identity(staff, "google", google(subject="g-2"), session_id=password_sid(staff))
    assert str(info.value.detail) == "Bu provayder allaqachon bog‘langan."
    with pytest.raises(Conflict) as info:
        services.link_identity(other, "google", google(subject="g-1"), session_id=password_sid(other))
    assert str(info.value.detail) == "Bu ijtimoiy hisob allaqachon bog‘langan."
    assert info.value.get_codes() == "conflict"
    assert SocialIdentity.objects.count() == 1
    assert AuditEntry.objects.filter(action="auth.social_linked").count() == 1
    # another provider for the same staff is fine
    services.link_identity(staff, "facebook", facebook(), session_id=password_sid(staff))
    assert SocialIdentity.objects.filter(staff=staff).count() == 2


def test_unlink_identity_deletes_and_audits(staff: StaffUser) -> None:
    link(staff, google(subject="g-9"))
    services.unlink_identity(staff, "google")
    assert SocialIdentity.objects.count() == 0
    entry = AuditEntry.objects.get(action="auth.social_unlinked")
    assert entry.after == {"provider": "google", "subject": "g-9"}
    with pytest.raises(NotFound):
        services.unlink_identity(staff, "google")
    with pytest.raises(NotFound):
        services.unlink_identity(staff, "facebook")


def test_link_identity_lost_race_is_conflict(staff: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent links of one subject: the loser hits the unique constraint and still answers 409."""
    other = StaffUserFactory.create()
    services.link_identity(other, "google", google(subject="g-1"), session_id=password_sid(other))
    monkeypatch.setattr(QuerySet, "exists", lambda self: False)  # the pre-checks "miss" the winner's row
    with pytest.raises(Conflict) as info:
        services.link_identity(staff, "google", google(subject="g-1"), session_id=password_sid(staff))
    assert str(info.value.detail) == "Bu ijtimoiy hisob allaqachon bog‘langan."
    assert SocialIdentity.objects.count() == 1
