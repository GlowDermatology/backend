"""End to end (FND-1): a clinic, a service, a specialist, its offer and its hours entered through the admin
API become bookable through the customer API at once (free slots + ``POST appointments/``); later schedule
changes keep the booking and surface it as a conflict; deactivating the specialist needs ``force``."""

import uuid
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.appointments.models import Appointment
from glowderma.core.models import AuditEntry

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]  # Saturday 24 Oct 2026 12:05

ADMIN = "/api/v1/admin/"
MONDAY = "2026-10-26"


def ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.content
    return dict(response.json())


def slot_starts(client: APIClient, specialist_id: int, service_id: int) -> list[str]:
    body = ok(
        client.get(
            f"/api/v1/specialists/{specialist_id}/slots/", {"date": MONDAY, "service_id": str(service_id)}
        )
    )
    return [slot["start"] for slot in body["slots"]]


def test_a_specialist_set_up_by_staff_is_bookable_in_the_app(
    staff_client: APIClient, customer_client: APIClient, public_client: APIClient
) -> None:
    # staff: the clinic, a service, the specialist, its offer and its hours
    ok(staff_client.patch(f"{ADMIN}location/", {"name": {"uz": "Glow Derma"}, "address": {"uz": "Toshkent"}}))
    service = ok(
        staff_client.post(
            f"{ADMIN}services/",
            {"name": {"uz": "Lazer epilyatsiya"}, "category": "cosmetology", "default_duration_min": 30},
        ),
        201,
    )
    specialist = ok(
        staff_client.post(
            f"{ADMIN}specialists/",
            {
                "name": "Malika Usmonova",
                "role": "cosmetologist",
                "qualifications": {"uz": "Kosmetolog diplomi"},
            },
        ),
        201,
    )
    pk, service_id = specialist["id"], service["id"]
    offer = {"services": [{"service_id": service_id, "price": 175_000, "duration_min": 45}]}
    ok(staff_client.put(f"{ADMIN}specialists/{pk}/services/", offer))
    week = {"days": [{"weekday": 0, "start": "09:00", "end": "13:00"}]}
    ok(staff_client.put(f"{ADMIN}specialists/{pk}/working-hours/", week))

    # created inactive: not offered to customers yet
    assert ok(public_client.get("/api/v1/specialists/"))["results"] == []
    assert (
        public_client.get(
            f"/api/v1/specialists/{pk}/slots/", {"date": MONDAY, "service_id": service_id}
        ).status_code
        == 404
    )
    ok(staff_client.patch(f"{ADMIN}specialists/{pk}/", {"is_active": True, "slot_step_min": 15}))

    # the app sees it at once
    [listed] = ok(public_client.get("/api/v1/specialists/"))["results"]
    assert (listed["id"], listed["name"], listed["qualifications"]) == (
        pk,
        "Malika Usmonova",
        "Kosmetolog diplomi",
    )
    assert listed["services"] == [
        {
            "id": service_id,
            "name": "Lazer epilyatsiya",
            "short_name": "Lazer epilyatsiya",
            "duration_min": 45,
            "price": 175_000,
        }
    ]
    assert listed["location"]["name"] == "Glow Derma" and listed["slot_step_min"] == 15
    [offered] = ok(public_client.get("/api/v1/services/"))["results"]
    assert (offered["category"], offered["price_from"], offered["specialist_ids"]) == (
        "cosmetology",
        175_000,
        [pk],
    )
    starts = slot_starts(public_client, pk, service_id)
    assert starts[0] == "09:00" and starts[-1] == "12:15" and len(starts) == 14  # 15-minute grid, 45 minutes

    # the customer books
    booked = ok(
        customer_client.post(
            "/api/v1/appointments/",
            {"specialist_id": pk, "service_id": service_id, "start": f"{MONDAY}T10:00:00+05:00"},
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        ),
        201,
    )
    assert (booked["status"], booked["price"], booked["duration_min"]) == ("requested", 175_000, 45)
    assert booked["end"] == f"{MONDAY}T10:45:00+05:00"
    after = slot_starts(public_client, pk, service_id)
    assert "10:00" not in after and "09:30" not in after and "09:15" in after and "10:45" in after
    board = ok(staff_client.get(f"{ADMIN}appointments/", {"date": MONDAY}))
    assert [(s["id"], s["working_hours"]) for s in board["specialists"]] == [
        (pk, {"start": "09:00", "end": "13:00"})
    ]
    assert [a["id"] for a in board["appointments"]] == [booked["id"]]
    assert ok(staff_client.get(f"{ADMIN}specialists/{pk}/"))["upcoming_appointments"] == 1

    # a block removes slots and leaves the booking alone; shrinking the hours turns it into a conflict
    block = {"start": f"{MONDAY}T11:00:00+05:00", "end": f"{MONDAY}T12:00:00+05:00", "kind": "busy"}
    ok(staff_client.post(f"{ADMIN}specialists/{pk}/blocks/", block), 201)
    assert not {"10:15", "11:00", "11:30"} & set(slot_starts(public_client, pk, service_id))
    assert ok(staff_client.get(f"{ADMIN}specialists/{pk}/conflicts/"))["results"] == []
    ok(
        staff_client.put(
            f"{ADMIN}specialists/{pk}/working-hours/",
            {"days": [{"weekday": 0, "start": "09:00", "end": "10:00"}]},
        )
    )
    conflicts = ok(staff_client.get(f"{ADMIN}specialists/{pk}/conflicts/"))["results"]
    assert [c["id"] for c in conflicts] == [booked["id"]]
    assert Appointment.objects.get(pk=booked["id"]).status == "requested"  # kept as booked

    # deactivation: refused while the booking is ahead, then forced; the app stops offering the specialist
    refused = staff_client.patch(f"{ADMIN}specialists/{pk}/", {"is_active": False})
    assert refused.status_code == 409 and "1 ta kelgusi qabul" in refused.json()["message"]
    ok(staff_client.patch(f"{ADMIN}specialists/{pk}/", {"is_active": False, "force": True}))
    assert public_client.get(f"/api/v1/specialists/{pk}/").status_code == 404
    assert ok(customer_client.get(f"/api/v1/me/appointments/{booked['id']}/"))["status"] == "requested"
    board = ok(staff_client.get(f"{ADMIN}appointments/", {"date": MONDAY}))
    assert [(s["id"], s["is_active"]) for s in board["specialists"]] == [(pk, False)]
    actions = list(AuditEntry.objects.order_by("id").values_list("action", flat=True))
    assert actions == [
        "location.created",
        "service.created",
        "professional.created",
        "professional.services_updated",
        "schedule.hours_updated",
        "professional.activated",
        "appointment.requested",
        "schedule.block_created",
        "schedule.hours_updated",
        "professional.deactivated",
    ]
