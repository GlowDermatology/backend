"""professionals.seed (design 03 §7.6): idempotent, keyed by natural ids; the design specialists and hours."""

from datetime import datetime, time

import pytest

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import enums, seeding
from glowderma.core.seeding import SeedContext, discover_seeders, registered_seeders
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import seeds
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
    Service,
    WorkingHours,
)
from glowderma.professionals.seeds import seed_professionals
from glowderma.professionals.selectors import DayBlock, day_schedules

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)

pytestmark = pytest.mark.django_db


def counts() -> tuple[int, ...]:
    return (
        Location.objects.count(),
        Professional.objects.count(),
        Service.objects.count(),
        ProfessionalService.objects.count(),
        WorkingHours.objects.count(),
        BlockedPeriod.objects.count(),
    )


def test_seed_is_idempotent_and_reproduces_the_design() -> None:
    admin = StaffUserFactory.create(email=seeds.SEED_ADMIN_EMAIL)
    seed_professionals(CONTEXT)
    assert counts() == (1, 3, 5, 7, 18, 1)
    seed_professionals(CONTEXT)
    assert counts() == (1, 3, 5, 7, 18, 1)

    location = Location.objects.get(pk=1)
    assert location.name_uz == "Glow Derma klinikasi" and location.address_uz.startswith(
        "Toshkent, Chilonzor"
    )
    assert list(Professional.objects.values_list("id", "name", "role", "position")) == [
        (1, "Nodira Rahimova", enums.SpecialistRole.DERMATOLOGIST, 0),
        (2, "Jasur Aliyev", enums.SpecialistRole.DERMATOLOGIST, 1),
        (3, "Dilnoza Yusupova", enums.SpecialistRole.COSMETOLOGIST, 2),
    ]
    assert all(p.location_id == 1 for p in Professional.objects.all())
    assert list(Service.objects.values_list("id", "name_uz", "short_name_uz", "default_duration_min")) == [
        (1, "Dermatolog konsultatsiyasi", "Konsultatsiya", 30),
        (2, "Qayta ko‘rik", "Qayta ko‘rik", 20),
        (3, "Yuzni chuqur tozalash", "Yuzni chuqur tozalash", 75),
        (4, "Kimyoviy pilling", "Kimyoviy pilling", 60),
        (5, "Namlantiruvchi parvarish", "Namlantiruvchi parvarish", 60),
    ]
    links = {
        (pid, sid): (price, duration)
        for pid, sid, price, duration in ProfessionalService.objects.values_list(
            "professional_id", "service_id", "price", "duration_min"
        )
    }
    assert links == {
        (1, 1): (150_000, 30),
        (1, 2): (100_000, 20),
        (2, 1): (120_000, 30),
        (2, 2): (90_000, 20),
        (3, 3): (320_000, 75),
        (3, 4): (380_000, 60),
        (3, 5): (260_000, 60),
    }
    for professional_id in (1, 2, 3):
        rows = list(WorkingHours.objects.filter(professional_id=professional_id))
        assert [r.weekday for r in rows] == [0, 1, 2, 3, 4, 5]  # Mon–Sat, no Sunday
        assert all((r.start, r.end) == (time(9, 0), time(18, 0)) for r in rows)
        breaks = {(r.break_start, r.break_end) for r in rows}
        assert breaks == ({(time(13, 0), time(14, 0))} if professional_id == 2 else {(None, None)})
    block = BlockedPeriod.objects.get()
    assert (block.professional_id, block.kind, block.label, block.created_by) == (
        1,
        enums.BlockKind.BUSY,
        "Band qilingan",
        admin,
    )
    assert block.start == datetime(2026, 10, 26, 16, 0, tzinfo=TASHKENT)
    assert block.end == datetime(2026, 10, 26, 16, 30, tzinfo=TASHKENT)
    assert block.created_at == ANCHOR


def test_seeded_day_board_matches_the_contract_example() -> None:
    seed_professionals(CONTEXT)
    schedules = day_schedules(datetime(2026, 10, 26, tzinfo=TASHKENT).date())  # the design Monday
    assert [s.professional.name for s in schedules] == ["Nodira Rahimova", "Jasur Aliyev", "Dilnoza Yusupova"]
    assert all(s.working_hours == ("09:00", "18:00") for s in schedules)
    assert schedules[0].blocks == [DayBlock("16:00", "16:30", enums.BlockKind.BUSY, "Band qilingan")]
    assert schedules[1].blocks == [DayBlock("13:00", "14:00", enums.BlockKind.OFF, None)]
    assert schedules[2].blocks == []
    sunday = day_schedules(datetime(2026, 10, 25, tzinfo=TASHKENT).date())
    assert all(s.working_hours is None for s in sunday)


def test_seed_without_the_admin_leaves_created_by_null() -> None:
    seed_professionals(CONTEXT)
    assert BlockedPeriod.objects.get().created_by is None


def test_registered_after_catalog() -> None:
    discover_seeders()  # imports every module's seeds.py (a static sibling import would break the layering)
    orders = {name: order for name, order, _ in registered_seeders()}
    assert orders["professionals"] == 50 and orders["professionals"] > orders["catalog"]
