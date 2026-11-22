"""Demo specialists (design 03 §7.6): a location, three professionals (placeholder photos), their own
services with prices, hours, a block.

Idempotent by natural keys: location ``name_uz``, professional ``name``, service ``(professional,
name_uz)``, hours ``(professional, weekday)``, block ``(professional, start)``. Design rows take their mock
ids (professionals 1–3, services 1–7, location 1) explicitly; ``seed_demo`` re-syncs the sequences.
"""

from datetime import datetime, time

from django.db import transaction
from django.db.models import Max

from glowderma.accounts.models import StaffUser
from glowderma.core import enums
from glowderma.core.seeding import SeedContext, placeholder_art, register_seeder
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import services as professional_services
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    Service,
    WorkingHours,
)

SEED_ADMIN_EMAIL = "admin@glowderma.uz"
LOCATION_NAME = "Glow Derma klinikasi"
# = SiteSettings.pickup_address_uz of site_config.seed (03 §7.4)
LOCATION_ADDRESS = "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12"
LOCATION_PHONE = "+998712000000"

# placeholder photo tone per professional id (core.seeding.placeholder_art "portrait", 1200×1500)
PHOTO_TONES: dict[int, str] = {1: "default", 2: "dark-cap", 3: "night"}
# id, name, role
PROFESSIONALS: tuple[tuple[int, str, str], ...] = (
    (1, "Nodira Rahimova", enums.SpecialistRole.DERMATOLOGIST),
    (2, "Jasur Aliyev", enums.SpecialistRole.DERMATOLOGIST),
    (3, "Dilnoza Yusupova", enums.SpecialistRole.COSMETOLOGIST),
)
DERMATOLOGY, COSMETOLOGY = enums.ServiceCategory.DERMATOLOGY, enums.ServiceCategory.COSMETOLOGY
# id, professional id, name_uz, short_name_uz, duration, category (FND-7), price: every service belongs to
# one specialist (Nodira 1–2, Dilnoza 3–5, Jasur 6–7)
SERVICES: tuple[tuple[int, int, str, str, int, str, int], ...] = (
    (1, 1, "Dermatolog konsultatsiyasi", "Konsultatsiya", 30, DERMATOLOGY, 150_000),
    (2, 1, "Qayta ko‘rik", "Qayta ko‘rik", 20, DERMATOLOGY, 100_000),
    (3, 3, "Yuzni chuqur tozalash", "Yuzni chuqur tozalash", 75, COSMETOLOGY, 320_000),
    (4, 3, "Kimyoviy pilling", "Kimyoviy pilling", 60, COSMETOLOGY, 380_000),
    (5, 3, "Namlantiruvchi parvarish", "Namlantiruvchi parvarish", 60, COSMETOLOGY, 260_000),
    (6, 2, "Dermatolog konsultatsiyasi", "Konsultatsiya", 30, DERMATOLOGY, 120_000),
    (7, 2, "Qayta ko‘rik", "Qayta ko‘rik", 20, DERMATOLOGY, 90_000),
)
WORKING_DAYS = range(6)  # Monday–Saturday; the mock skips Sundays
DAY_START, DAY_END = time(9, 0), time(18, 0)
JASUR_BREAK = (time(13, 0), time(14, 0))  # renders as the ``off`` block with label null (contract §5.6)
NODIRA_BLOCK = (
    datetime(2026, 10, 26, 16, 0, tzinfo=TASHKENT),
    datetime(2026, 10, 26, 16, 30, tzinfo=TASHKENT),
    enums.BlockKind.BUSY,
    "Band qilingan",
)


def _seed_location() -> Location:
    location = Location.objects.filter(name_uz=LOCATION_NAME).first()
    if location is None:
        location = Location.objects.create(
            id=1, name_uz=LOCATION_NAME, address_uz=LOCATION_ADDRESS, phone=LOCATION_PHONE
        )
    return location


def _seed_professionals(location: Location) -> dict[int, Professional]:
    rows: dict[int, Professional] = {}
    for position, (pk, name, role) in enumerate(PROFESSIONALS):
        professional = Professional.objects.filter(name=name).first()
        if professional is None:
            # Every profile is owned by a Specialist account; demo accounts stay inactive without a password
            # until staff change the login email and send an invitation.
            # Explicit ids: earlier seeders insert fixed ids and the sequences are re-synced only at the end.
            next_id = (StaffUser.objects.aggregate(top=Max("id"))["top"] or 0) + 1
            account, _ = StaffUser.objects.get_or_create(
                email=f"specialist-{pk}@demo.glowderma.invalid",
                defaults={
                    "id": next_id,
                    "full_name": name,
                    "role": enums.StaffRole.SPECIALIST,
                    "is_active": False,
                    "password": "!",  # noqa: S106 - unusable
                },
            )
            professional = Professional.objects.create(
                id=pk,
                location=location,
                staff_user=account,
                name=name,
                role=role,
                position=position,
                is_active=True,
            )
        rows[pk] = professional
    return rows


def _seed_photos(professionals: dict[int, Professional]) -> None:
    """A demo photo for each seeded professional without one (a photo staff uploaded is kept), stored through
    the same processing and naming as an admin upload."""
    for key, professional in professionals.items():
        if not professional.photo:
            professional_services.store_photo(
                professional, placeholder_art("portrait", PHOTO_TONES.get(key, "default"))
            )


def _seed_services(professionals: dict[int, Professional]) -> dict[int, Service]:
    rows: dict[int, Service] = {}
    positions: dict[int, int] = {}
    for pk, owner, name_uz, short_name_uz, duration, category, price in SERVICES:
        professional = professionals[owner]
        position = positions.get(owner, 0)
        positions[owner] = position + 1
        service = Service.objects.filter(professional=professional, name_uz=name_uz).first()
        if service is None:
            service = Service.objects.create(
                id=pk,
                professional=professional,
                name_uz=name_uz,
                short_name_uz=short_name_uz,
                duration_min=duration,
                price=price,
                category=category,
                position=position,
            )
        rows[pk] = service
    return rows


def _seed_hours(professionals: dict[int, Professional]) -> None:
    for key, professional in professionals.items():
        break_start, break_end = JASUR_BREAK if key == 2 else (None, None)
        for weekday in WORKING_DAYS:
            WorkingHours.objects.get_or_create(
                professional=professional,
                weekday=weekday,
                defaults={
                    "location_id": professional.clinic_id,
                    "start": DAY_START,
                    "end": DAY_END,
                    "break_start": break_start,
                    "break_end": break_end,
                },
            )


def _seed_blocks(
    professionals: dict[int, Professional], admin: StaffUser | None, created_at: datetime
) -> None:
    start, end, kind, label = NODIRA_BLOCK
    BlockedPeriod.objects.get_or_create(
        professional=professionals[1],
        start=start,
        defaults={"end": end, "kind": kind, "label": label, "created_by": admin, "created_at": created_at},
    )


@register_seeder("professionals", order=50)
def seed_professionals(context: SeedContext) -> None:
    with transaction.atomic():
        admin = StaffUser.objects.filter(email=SEED_ADMIN_EMAIL).first()
        location = _seed_location()
        professionals = _seed_professionals(location)
        _seed_photos(professionals)
        _seed_services(professionals)
        _seed_hours(professionals)
        _seed_blocks(professionals, admin, context.anchor)
