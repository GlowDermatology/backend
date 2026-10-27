"""``manage.py seed_demo`` over every seeder: twice → identical row counts (design 03 §10), the appointments
and notifications rows on top of the lower layers, and the id sequences re-synced past the explicit ids."""

from datetime import datetime
from io import StringIO

import pytest
from django.core.management import call_command

from glowderma.accounts.models import Customer
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.core import seeding
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications.models import Campaign, CustomerSegment, SegmentMember
from glowderma.professionals.models import Professional


def counts() -> tuple[int, ...]:
    return (
        Appointment.objects.count(),
        AppointmentEvent.objects.count(),
        CustomerSegment.objects.count(),
        SegmentMember.objects.count(),
        Campaign.objects.count(),
    )


@pytest.mark.django_db
def test_seed_demo_runs_both_seeders_after_the_lower_layers_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    out = StringIO()
    call_command("seed_demo", force=True, stdout=out)
    ran = out.getvalue().split("ran: ", 1)[1].splitlines()[0].split(", ")
    assert ran.index("professionals") < ran.index("appointments") < ran.index("notifications")
    assert ran[-2:] == ["appointments", "notifications"]
    first = counts()
    assert first == (45, 98, 2, 2, 4)
    assert Campaign.objects.get(pk=3).link_product is not None  # catalog's product 4 was there
    assert Campaign.objects.get(pk=3).link_product.name_uz == "Seramidli namlantiruvchi krem"  # type: ignore[union-attr]
    assert Appointment.objects.get(pk=3).created_by_id == 1
    call_command("seed_demo", force=True, stdout=StringIO())
    assert counts() == first
    # sequences re-synced: the next rows continue after the seeded ids
    anchor = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
    created = Appointment.objects.create(
        professional=Professional.objects.get(pk=1),
        service_id=1,
        customer=Customer.objects.get(pk=1),
        start=datetime(2026, 11, 2, 9, 0, tzinfo=TASHKENT),
        end=datetime(2026, 11, 2, 9, 30, tzinfo=TASHKENT),
        duration_min=30,
        price=150_000,
        requested_via="app",
        requested_at=anchor,
    )
    assert created.pk > 1000 + 35
    assert CustomerSegment.objects.create(name="Yangi guruh").pk == 3
    assert (
        Campaign.objects.create(kind="general", title="Keyingi", body="Keyingi xabar", audience_type="all").pk
        == 5
    )
