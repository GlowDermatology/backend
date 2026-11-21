"""Specialist accounts and their owned profiles, end to end.

One staff sign-in page, three roles (Staff, Manager, Specialist). A manager invites a Specialist: the account
and its private draft profile are created together. The specialist signs in on the shared login, completes
their public name, photo and bio and sets availability for the clinics staff assigned; staff publish once the
profile is complete and customers can book. Staff → Edit changes the role explicitly (sessions revoked, the
profile reused, appointments never moved). Every ``specialist/`` route resolves the profile from the signed-in
account only.
"""

import uuid
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts import tokens
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import TEST_PASSWORD, StaffUserFactory
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums, timeutils
from glowderma.professionals.models import Professional, WorkingHours
from glowderma.professionals.tests.factories import (
    LocationFactory,
    ProfessionalFactory,
    ServiceFactory,
    WorkingHoursFactory,
)

pytestmark = pytest.mark.django_db

ADMIN = "/api/v1/admin/"
OWN = ADMIN + "specialist/"


def next_monday() -> str:
    today = timeutils.local_date(timeutils.now())
    return (today + timedelta(days=7 - today.weekday())).isoformat()


def keyed() -> dict[str, Any]:
    return {"format": "json", "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def panel(user: StaffUser) -> APIClient:
    session, _ = account_services.start_staff_session(user, device_label="test")
    bearer, _ = tokens.issue_access(session)
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bearer}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, getattr(response, "data", response.content)
    return response.data


def field_errors(response: Any) -> list[tuple[str, str]]:
    return [(error["field"], error["code"]) for error in response.data["errors"]]


def photo() -> SimpleUploadedFile:
    buffer = BytesIO()
    Image.new("RGB", (40, 40), (200, 120, 90)).save(buffer, format="PNG")
    return SimpleUploadedFile("me.png", buffer.getvalue(), content_type="image/png")


def manager() -> APIClient:
    return panel(StaffUserFactory.create(is_manager=True))


def invite(client: APIClient, email: str = "amina@clinic.example", **extra: Any) -> Any:
    body = {"email": email, "full_name": "Amina Karimova", "delivery": "temporary_password", **extra}
    return client.post(ADMIN + "specialists/invite/", body, **keyed())


def role(client: APIClient, account_id: int, target: str, **headers: Any) -> Any:
    return client.post(
        f"{ADMIN}staff/{account_id}/specialist-role/",
        {"role": target, "confirmed": True},
        **(headers or keyed()),
    )


def sign_in(email: str, password: str) -> APIClient:
    """The shared staff login, then the forced password change of an invited account."""
    client = APIClient()
    client.credentials(HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET)
    login = client.post(ADMIN + "auth/login/", {"email": email, "password": password}, format="json")
    assert login.status_code == 200, login.data
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {login.data['access']}",
        HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET,
    )
    return client


# --- 1. invite: account and draft profile together ----------------------------------------------------------


def test_invite_creates_account_and_draft_profile_together(staff_client: APIClient) -> None:
    boss = manager()
    assert invite(staff_client).status_code == 403  # a Staff account cannot provision
    headers = keyed()
    body = {
        "email": " Amina@Clinic.example ",
        "full_name": "Amina Karimova",
        "delivery": "temporary_password",
    }
    created = ok(boss.post(ADMIN + "specialists/invite/", body, **headers), 201)
    assert created["staff"]["role"] == "specialist" and created["staff"]["is_manager"] is False
    assert created["temporary_password"]
    profile = Professional.objects.get(pk=created["specialist_id"])
    assert profile.staff_user_id == created["staff"]["id"]
    assert (profile.name, profile.is_active, profile.location_id, profile.role) == (
        "Amina Karimova",
        False,
        None,
        "",
    )

    # a retried request replays; a second invite of the same email creates nothing
    assert boss.post(ADMIN + "specialists/invite/", body, **headers).data == created
    duplicate = invite(boss, "amina@clinic.example")
    assert duplicate.status_code == 400 and ("email", "unique") in field_errors(duplicate)
    assert StaffUser.objects.filter(email="amina@clinic.example").count() == 1
    assert Professional.objects.filter(staff_user_id=created["staff"]["id"]).count() == 1

    # resending the invitation and changing the login email keep the same account and profile
    account_id = created["staff"]["id"]
    ok(boss.post(f"{ADMIN}staff/{account_id}/reset-password/", {}, **keyed()))
    ok(boss.patch(f"{ADMIN}staff/{account_id}/", {"email": "amina.k@clinic.example"}, format="json"))
    assert Professional.objects.get(staff_user_id=account_id).pk == profile.pk
    taken = boss.patch(f"{ADMIN}staff/{account_id}/", {"email": "AMINA.K@clinic.example"}, format="json")
    assert taken.status_code == 200  # the account's own address again: no change
    StaffUserFactory.create(email="other@clinic.example")
    clash = boss.patch(f"{ADMIN}staff/{account_id}/", {"email": "other@clinic.example"}, format="json")
    assert clash.status_code == 400 and ("email", "unique") in field_errors(clash)


def test_profiles_are_never_created_or_relinked_by_hand(staff_client: APIClient) -> None:
    doctor = ProfessionalFactory.create()
    other = StaffUserFactory.create(role=enums.StaffRole.SPECIALIST)
    assert staff_client.post(ADMIN + "specialists/", {"name": "Hand made"}, format="json").status_code == 405
    ok(staff_client.patch(f"{ADMIN}specialists/{doctor.pk}/", {"staff_user_id": other.pk}, format="json"))
    doctor.refresh_from_db()
    assert doctor.staff_user_id != other.pk


# --- 2. the specialist completes the draft; staff publish ---------------------------------------------------


def test_full_flow_from_invitation_to_customer_booking(
    staff_client: APIClient, customer_client: APIClient, public_client: APIClient
) -> None:
    boss = manager()
    monday = next_monday()
    created = ok(invite(boss), 201)
    pk = created["specialist_id"]

    # the shared staff login; an invited account must set its own password first
    client = sign_in("amina@clinic.example", created["temporary_password"])
    assert client.get(OWN + "profile/").status_code == 403  # password change pending
    ok(
        client.post(
            ADMIN + "auth/change-password/",
            {"current_password": created["temporary_password"], "new_password": TEST_PASSWORD + "-x9"},
            format="json",
        ),
        204,
    )
    me = ok(client.get(ADMIN + "auth/me/"))
    assert me["role"] == "specialist"
    client = sign_in("amina@clinic.example", TEST_PASSWORD + "-x9")

    draft = ok(client.get(OWN + "profile/"))
    assert draft["is_active"] is False and draft["account"]["email"] == "amina@clinic.example"
    assert set(draft["publish_blockers"]) == {
        "discipline",
        "bio",
        "photo",
        "clinic",
        "service",
        "availability",
    }
    assert ok(client.get(OWN + "availability/"))["clinics"] == []  # no clinic assigned yet
    assert public_client.get(f"/api/v1/specialists/{pk}/").status_code == 404  # private draft

    # the specialist: public name (may differ from the login name), bio, photo
    edited = ok(
        client.patch(OWN + "profile/", {"name": "Dr. Amina", "bio": {"uz": "Teri shifokori"}}, **keyed())
    )
    assert edited["name"] == "Dr. Amina" and edited["bio"]["uz"] == "Teri shifokori"
    assert StaffUser.objects.get(pk=created["staff"]["id"]).full_name == "Amina Karimova"
    ok(client.patch(OWN + "profile/", {"bio": {"ru": "Дерматолог"}}, **keyed()))
    assert Professional.objects.get(pk=pk).bio_uz == "Teri shifokori"  # languages merge one by one
    ok(client.put(OWN + "profile/photo/", {"file": photo()}, format="multipart"))
    # a specialist cannot publish, assign clinics or services through their own route
    ok(client.patch(OWN + "profile/", {"is_active": True, "location_id": 1}, **keyed()))
    assert Professional.objects.get(pk=pk).is_active is False

    # staff assign the clinic, the discipline and a service
    clinic = LocationFactory.create()
    service = ServiceFactory.create()
    ok(
        staff_client.patch(
            f"{ADMIN}specialists/{pk}/",
            {"role": "dermatologist", "location_id": clinic.pk, "location_ids": [clinic.pk]},
            format="json",
        )
    )
    offer = {"services": [{"service_id": service.pk, "price": 150_000}]}
    ok(staff_client.put(f"{ADMIN}specialists/{pk}/services/", offer, format="json"))
    blocked = staff_client.patch(f"{ADMIN}specialists/{pk}/", {"is_active": True}, format="json")
    assert blocked.status_code == 409 and "ish vaqti" in str(blocked.data["message"])

    # the specialist sets availability, only at their assigned clinics
    other_clinic = LocationFactory.create()
    foreign = {"days": [{"weekday": 0, "start": "09:00", "end": "13:00", "location_id": other_clinic.pk}]}
    assert client.put(OWN + "weekly-hours/", foreign, **keyed()).status_code == 400
    week = {"days": [{"weekday": 0, "start": "09:00", "end": "13:00", "location_id": clinic.pk}]}
    assert client.put(OWN + "weekly-hours/", week, **keyed()).status_code == 204
    assert ok(client.get(OWN + "profile/"))["publish_blockers"] == []

    # publication is separate from login activation
    published = ok(staff_client.patch(f"{ADMIN}specialists/{pk}/", {"is_active": True}, format="json"))
    assert published["is_active"] is True and published["account"]["is_active"] is True
    [listed] = ok(public_client.get("/api/v1/specialists/"))["results"]
    assert listed["id"] == pk and listed["name"] == "Dr. Amina"

    booked = customer_client.post(
        "/api/v1/appointments/",
        {
            "specialist_id": pk,
            "service_id": service.pk,
            "location_id": clinic.pk,
            "start": f"{monday}T10:00:00+05:00",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert booked.status_code == 201, booked.data
    calendar = ok(client.get(OWN + "calendar/", {"from": monday, "to": monday}))
    assert [row["id"] for row in calendar["appointments"]] == [booked.data["id"]]


def test_specialists_list_includes_drafts_with_their_owner(staff_client: APIClient) -> None:
    published = ProfessionalFactory.create()
    draft = ProfessionalFactory.create(is_active=False, location=None, role="")
    rows = {row["id"]: row for row in ok(staff_client.get(ADMIN + "specialists/"))["results"]}
    assert rows[draft.pk]["is_active"] is False and rows[draft.pk]["location_id"] is None
    assert rows[draft.pk]["account"]["id"] == draft.staff_user_id
    assert "clinic" in rows[draft.pk]["publish_blockers"]
    assert rows[published.pk]["is_active"] is True
    only_drafts = ok(staff_client.get(ADMIN + "specialists/", {"is_active": "false"}))["results"]
    assert [row["id"] for row in only_drafts] == [draft.pk]


# --- 3. explicit role changes -------------------------------------------------------------------------------


@pytest.mark.parametrize("was_manager", [False, True])
def test_staff_to_specialist_creates_or_reuses_one_profile(
    staff_client: APIClient, was_manager: bool
) -> None:
    boss = manager()
    account = StaffUserFactory.create(is_manager=was_manager)
    before = panel(account)
    endpoint = f"{ADMIN}staff/{account.pk}/specialist-role/"
    assert (
        staff_client.post(endpoint, {"role": "specialist", "confirmed": True}, **keyed()).status_code == 403
    )
    unconfirmed = boss.post(endpoint, {"role": "specialist", "confirmed": False}, **keyed())
    assert unconfirmed.status_code == 400

    headers = keyed()
    converted = ok(role(boss, account.pk, "specialist", **headers))
    assert converted["account"]["role"] == "specialist" and converted["account"]["is_manager"] is False
    profile = Professional.objects.get(staff_user=account)
    assert converted["specialist_id"] == profile.pk and profile.is_active is False
    assert before.get(ADMIN + "auth/me/").status_code == 401  # sessions revoked

    # retries: the same key replays; a fresh retry is a no-op; never a second profile
    assert role(boss, account.pk, "specialist", **headers).data == converted
    assert ok(role(boss, account.pk, "specialist"))["specialist_id"] == profile.pk
    assert Professional.objects.filter(staff_user=account).count() == 1

    specialist = panel(account)
    assert specialist.get(OWN + "profile/").status_code == 200
    assert specialist.get(ADMIN + "orders/").status_code == 403


def test_specialist_to_staff_pauses_bookings_and_keeps_history(staff_client: APIClient) -> None:
    boss = manager()
    doctor = ProfessionalFactory.create()
    WorkingHoursFactory.create(professional=doctor, weekday=0)
    account = doctor.staff_user
    visit = AppointmentFactory.create(professional=doctor, start=timeutils.now() + timedelta(days=2))
    specialist = panel(account)

    demoted = ok(role(boss, account.pk, "staff"))
    assert demoted["account"]["role"] == "admin" and demoted["specialist_id"] == doctor.pk
    assert demoted["upcoming_appointments"] == 1
    assert specialist.get(ADMIN + "auth/me/").status_code == 401
    doctor.refresh_from_db()
    visit.refresh_from_db()
    assert doctor.staff_user_id == account.pk and doctor.is_active is False  # paused, still owned
    assert visit.professional_id == doctor.pk and Appointment.objects.filter(pk=visit.pk).exists()
    assert WorkingHours.objects.filter(professional=doctor).exists()

    staff = panel(account)
    assert staff.get(OWN + "profile/").status_code == 403  # specialist access ended
    assert staff.get(ADMIN + "orders/").status_code == 200
    detail = ok(staff_client.get(f"{ADMIN}specialists/{doctor.pk}/"))
    assert detail["publish_blockers"][:1] == ["account"]
    refused = staff_client.patch(f"{ADMIN}specialists/{doctor.pk}/", {"is_active": True}, format="json")
    assert refused.status_code == 409

    # back to Specialist: the same profile, still unpublished until staff review it
    returned = ok(role(boss, account.pk, "specialist"))
    assert returned["specialist_id"] == doctor.pk
    assert Professional.objects.filter(staff_user=account).count() == 1
    doctor.refresh_from_db()
    assert doctor.is_active is False
    review = ok(staff_client.get(f"{ADMIN}specialists/{doctor.pk}/"))
    assert "account" not in review["publish_blockers"]


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ({"is_superuser": True, "is_manager": True}, "The superuser role is protected."),
        ({"is_staff": True}, "Revoke this account’s operations console access first."),
        ({"is_active": False}, None),
    ],
)
def test_protected_accounts_keep_their_role(flags: dict[str, bool], message: str | None) -> None:
    boss = manager()
    target = StaffUserFactory.create(**flags)
    result = role(boss, target.pk, "specialist", HTTP_ACCEPT_LANGUAGE="en", **keyed())
    assert result.status_code == 409, result.data
    if message:
        assert result.data["message"] == message
    target.refresh_from_db()
    assert target.role == "admin" and not Professional.objects.filter(staff_user=target).exists()


def test_last_manager_and_own_account_are_protected() -> None:
    only = StaffUserFactory.create(is_manager=True)
    operator = panel(StaffUserFactory.create(is_superuser=True))  # may manage staff, is not a manager row
    assert role(operator, only.pk, "specialist").status_code == 409
    assert role(panel(only), only.pk, "specialist").status_code == 409
    only.refresh_from_db()
    assert only.is_manager and only.role == "admin"


# --- 4. the access boundary ---------------------------------------------------------------------------------


def test_a_specialist_reaches_only_their_own_profile_and_appointments() -> None:
    mine = ProfessionalFactory.create()
    theirs = ProfessionalFactory.create()
    client = panel(mine.staff_user)
    later = timeutils.now() + timedelta(days=2)
    own_visit = AppointmentFactory.create(professional=mine, start=later)
    foreign_visit = AppointmentFactory.create(professional=theirs, start=later)

    # the admin routes for profiles and appointments are closed to a specialist, also by direct request
    for path in (
        f"specialists/{theirs.pk}/",
        f"specialists/{mine.pk}/",
        "specialists/",
        "appointments/",
        "staff/",
    ):
        assert client.get(ADMIN + path).status_code == 403, path
    assert (
        client.patch(f"{ADMIN}specialists/{theirs.pk}/", {"name": "Hijacked"}, format="json").status_code
        == 403
    )
    assert client.put(f"{ADMIN}specialists/{theirs.pk}/photo/", {"file": photo()}).status_code == 403
    theirs_week = {"days": [{"weekday": 0, "start": "09:00", "end": "12:00"}]}
    assert (
        client.put(f"{ADMIN}specialists/{theirs.pk}/working-hours/", theirs_week, format="json").status_code
        == 403
    )
    assert client.post(ADMIN + "specialists/invite/", {}, format="json").status_code == 403

    # the specialist routes resolve the caller's own profile, whatever id is sent
    assert ok(client.get(OWN + "profile/"))["id"] == mine.pk
    ok(client.patch(OWN + "profile/", {"name": "Still me", "id": theirs.pk}, **keyed()))
    theirs.refresh_from_db()
    assert theirs.name != "Still me"
    confirm = client.post(f"{OWN}bookings/{foreign_visit.pk}/confirm/", {}, **keyed())
    assert confirm.status_code == 404
    assert client.post(f"{OWN}bookings/{own_visit.pk}/confirm/", {}, **keyed()).status_code == 204


def test_a_deactivated_login_closes_the_workspace_but_keeps_the_profile() -> None:
    doctor = ProfessionalFactory.create()
    client = panel(doctor.staff_user)
    boss = manager()
    ok(boss.post(f"{ADMIN}staff/{doctor.staff_user_id}/deactivate/", {}, **keyed()))
    assert client.get(OWN + "profile/").status_code == 401
    doctor.refresh_from_db()
    assert doctor.is_active is True  # publication is a separate control
