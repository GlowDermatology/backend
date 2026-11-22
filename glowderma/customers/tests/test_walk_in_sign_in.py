"""A walk-in booked by staff (``POST admin appointments/`` with a phone no customer holds and a ``name``) is a
customer without a password or consent. Their first sign-in is the SMS-code recovery of that phone
(``auth/send-code/`` ``reset_password`` → ``auth/verify-code/`` → ``auth/reset-password/``): it lands in the
same row, sets the password and keeps the staff-booked visit; ``me/`` then asks for the terms
(``update_required``) and ``me/consent/`` records them."""

from collections.abc import Iterator
from datetime import datetime, time, timedelta

import pytest
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.core import timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.customers.tests.p1_support import (
    NEW_PASSWORD,
    TERMS,
    app_client,
    bearer,
    publish_terms,
    reset_fakes,
)
from glowderma.professionals.tests.factories import ServiceFactory, WorkingHoursFactory

pytestmark = pytest.mark.django_db

PHONE = "+998907776655"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def test_a_walk_in_signs_in_by_sms_code_into_the_same_row_and_accepts_the_terms(
    staff_client: APIClient,
) -> None:
    publish_terms()
    service = ServiceFactory.create()
    day = timeutils.local_date(timeutils.now()) + timedelta(days=7)
    WorkingHoursFactory.create(
        professional=service.professional, weekday=day.weekday(), start=time(9), end=time(18)
    )
    start = datetime.combine(day, time(10), tzinfo=TASHKENT)
    booked = staff_client.post(
        "/api/v1/admin/appointments/",
        {
            "specialist_id": service.professional_id,
            "service_id": service.pk,
            "location_id": service.professional.location_id,
            "phone": PHONE,
            "name": "Yangi Mijoz",
            "start": start.isoformat(),
        },
        format="json",
    )
    assert booked.status_code == 201, booked.content
    walk_in = Customer.objects.get(phone=PHONE)
    assert (walk_in.password, walk_in.consent_at) == ("", None)

    app = app_client()
    # registering the phone again is refused: the account exists, so the app takes the recovery path
    taken = app.post("/api/v1/auth/send-code/", {"phone": PHONE, "purpose": "register"})
    assert (taken.status_code, taken.data["code"]) == (409, "phone_registered")
    sent = app.post("/api/v1/auth/send-code/", {"phone": PHONE, "purpose": "reset_password"})
    assert sent.status_code == 200, sent.data
    verified = app.post(
        "/api/v1/auth/verify-code/", {"phone": PHONE, "purpose": "reset_password", "code": "123456"}
    )
    assert verified.status_code == 200 and verified.data["customer_exists"] is True, verified.data
    signed = app.post(
        "/api/v1/auth/reset-password/",
        {"verification_token": verified.data["verification_token"], "password": NEW_PASSWORD},
    )
    assert signed.status_code == 200, signed.data
    assert signed.data["customer"]["id"] == walk_in.pk  # the same row, never a second account
    assert Customer.objects.filter(phone=PHONE).count() == 1
    walk_in.refresh_from_db()
    assert account_services.check_customer_password(walk_in, NEW_PASSWORD)

    me_client = bearer(
        app_client(key=signed.data["device_key"]), signed.data["access"], key=signed.data["device_key"]
    )
    me = me_client.get("/api/v1/me/")
    assert me.status_code == 200, me.data
    assert me.data["name"] == "Yangi Mijoz" and me.data["has_password"] is True
    assert me.data["consent"]["update_required"] is True and me.data["consent"]["accepted_at"] is None
    upcoming = me_client.get("/api/v1/me/appointments/", {"scope": "upcoming"})
    assert [row["id"] for row in upcoming.data["results"]] == [booked.data["id"]]
    accepted = me_client.post("/api/v1/me/consent/", {"terms_version": TERMS})
    assert accepted.status_code == 200 and accepted.data["consent"]["update_required"] is False
