"""professionals.services: validated writes with ``<model>.created|updated|deleted`` audit entries."""

from datetime import date, datetime, time
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from glowderma.accounts.models import StaffUser
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import services
from glowderma.professionals.models import (
    BlockedPeriod,
    Professional,
    ProfessionalService,
    WorkingHours,
    WorkingHoursOverride,
)
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    LocationFactory,
    ProfessionalFactory,
    ProfessionalServiceFactory,
    ServiceFactory,
    WorkingHoursFactory,
)

pytestmark = pytest.mark.django_db


def image_bytes(fmt: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 16), "#d6b2c4").save(output, format=fmt)
    return output.getvalue()


def actions() -> list[tuple[str, str, str]]:
    return list(AuditEntry.objects.order_by("id").values_list("action", "entity_type", "entity_id"))


def test_create_update_deactivate_professional_are_audited(staff: StaffUser) -> None:
    location = LocationFactory.create()
    professional = services.create_professional(
        location=location, name="Nodira Rahimova", role=enums.SpecialistRole.DERMATOLOGIST, actor=staff
    )
    assert professional.pk is not None and professional.is_active
    services.update_professional(professional, actor=staff, experience_years=12)
    services.deactivate_professional(professional, actor=staff)
    stored = Professional.objects.get(pk=professional.pk)
    assert stored.experience_years == 12 and stored.is_active is False
    pk = str(professional.pk)
    assert actions() == [
        ("professional.created", "professional", pk),
        ("professional.updated", "professional", pk),
        ("professional.updated", "professional", pk),
    ]
    created, updated, deactivated = AuditEntry.objects.order_by("id")
    assert created.before is None and created.actor_id == staff.pk
    assert created.after is not None and updated.before is not None and updated.after is not None
    assert deactivated.before is not None and deactivated.after is not None
    assert created.after["name"] == "Nodira Rahimova"
    assert updated.before["experience_years"] is None and updated.after["experience_years"] == 12
    assert deactivated.before["is_active"] is True and deactivated.after["is_active"] is False
    assert created.after["location_id"] == location.pk and created.after["photo"] == ""


def test_professional_photo_is_processed_and_content_addressed(staff: StaffUser) -> None:
    professional = services.create_professional(
        location=LocationFactory.create(),
        name="Dilnoza",
        role=enums.SpecialistRole.COSMETOLOGIST,
        actor=staff,
        photo=SimpleUploadedFile("me.png", image_bytes("PNG"), content_type="image/png"),
    )
    professional.refresh_from_db()
    name = professional.photo.name or ""
    assert name.startswith(f"professionals/{professional.pk}/") and name.endswith(".png")
    created = AuditEntry.objects.get(action="professional.created")
    assert created.after is not None and created.after["photo"] == name
    with pytest.raises(ValidationError, match="JPEG, PNG yoki WebP"):
        services.update_professional(
            professional, actor=staff, photo=SimpleUploadedFile("x.txt", b"not an image")
        )


def test_invariants_raise_validation_errors(staff: StaffUser) -> None:
    professional = ProfessionalFactory.create()
    with pytest.raises(ValidationError):  # start < end
        services.set_working_hours(professional, 0, time(18, 0), time(9, 0), actor=staff)
    with pytest.raises(ValidationError):  # break outside the day
        services.set_working_hours(
            professional,
            0,
            time(9, 0),
            time(18, 0),
            break_start=time(8, 0),
            break_end=time(9, 0),
            actor=staff,
        )
    with pytest.raises(ValidationError):  # weekday out of range
        services.set_working_hours(professional, 7, time(9, 0), time(18, 0), actor=staff)
    with pytest.raises(ValidationError):  # block end > start
        services.add_blocked_period(
            professional,
            datetime(2026, 10, 26, 16, tzinfo=TASHKENT),
            datetime(2026, 10, 26, 16, tzinfo=TASHKENT),
            enums.BlockKind.BUSY,
            actor=staff,
        )
    with pytest.raises(ValidationError):  # slot step not allowed
        services.update_professional(professional, actor=staff, slot_step_min=7)
    assert AuditEntry.objects.count() == 0


def test_set_working_hours_upserts_and_removes(staff: StaffUser) -> None:
    professional = ProfessionalFactory.create()
    row = services.set_working_hours(professional, 0, time(9, 0), time(18, 0), actor=staff)
    assert row is not None and row.pk is not None
    again = services.set_working_hours(
        professional, 0, time(10, 0), time(17, 0), break_start=time(13, 0), break_end=time(14, 0), actor=staff
    )
    assert again is not None and again.pk == row.pk
    assert WorkingHours.objects.get(pk=row.pk).break_start == time(13, 0)
    assert services.set_working_hours(professional, 0, None, None, actor=staff) is None
    assert not WorkingHours.objects.filter(pk=row.pk).exists()
    assert [a for a, _, _ in actions()] == [
        "working_hours.created",
        "working_hours.updated",
        "working_hours.deleted",
    ]
    deleted = AuditEntry.objects.get(action="working_hours.deleted")
    assert deleted.before is not None and deleted.before["break_start"] == "13:00:00"
    assert deleted.after is None


def test_override_blocks_and_links(staff: StaffUser) -> None:
    professional = ProfessionalFactory.create()
    service = ServiceFactory.create(default_duration_min=45)
    override = services.add_override(professional, date(2026, 11, 2), None, None, note="Ta’til", actor=staff)
    assert override.created_by == staff and override.start is None
    services.add_override(professional, date(2026, 11, 2), time(10, 0), time(14, 0), actor=staff)
    assert WorkingHoursOverride.objects.filter(professional=professional).count() == 1
    with pytest.raises(ValidationError):  # start without end
        services.add_override(professional, date(2026, 11, 3), time(10, 0), None, actor=staff)

    block = services.add_blocked_period(
        professional,
        datetime(2026, 10, 26, 16, tzinfo=TASHKENT),
        datetime(2026, 10, 26, 16, 30, tzinfo=TASHKENT),
        enums.BlockKind.BUSY,
        label="Band qilingan",
        actor=staff,
    )
    assert block.created_by == staff
    services.remove_blocked_period(block, actor=staff)
    assert not BlockedPeriod.objects.filter(pk=block.pk).exists()

    link = services.link_service(professional, service, price=150_000, actor=staff)
    assert (link.duration_min, link.is_active) == (45, True)
    relinked = services.link_service(professional, service, price=160_000, duration_min=30, actor=staff)
    assert relinked.pk == link.pk and ProfessionalService.objects.get(pk=link.pk).duration_min == 30
    with pytest.raises(ValidationError):
        services.link_service(professional, service, price=-1, actor=staff)
    services.unlink_service(relinked, actor=staff)
    assert [a for a, _, _ in actions()] == [
        "working_hours_override.created",
        "working_hours_override.updated",
        "blocked_period.created",
        "blocked_period.deleted",
        "professional_service.created",
        "professional_service.updated",
        "professional_service.deleted",
    ]


def test_delete_protected_row_is_a_friendly_error(staff: StaffUser) -> None:
    link = ProfessionalServiceFactory.create()
    with pytest.raises(ValidationError, match="bog‘langan"):
        services.delete_row(link.service, actor=staff)
    assert not AuditEntry.objects.filter(action="service.deleted").exists()
    services.delete_row(link, actor=staff)
    services.delete_row(link.service, actor=staff)
    assert AuditEntry.objects.filter(action="service.deleted").exists()


def test_unknown_model_is_refused(staff: StaffUser) -> None:
    with pytest.raises(ValueError, match="StaffUser"):
        services.save_row(staff, actor=staff)


def test_snapshot_renders_times_and_foreign_keys() -> None:
    hours = WorkingHoursFactory.create(weekday=2)
    block = BlockedPeriodFactory.create()
    assert services.snapshot(hours) == {
        "id": hours.pk,
        "professional_id": hours.professional_id,
        "weekday": 2,
        "start": "09:00:00",
        "end": "18:00:00",
        "break_start": None,
        "break_end": None,
    }
    assert services.snapshot(block)["start"] == "2026-10-26T16:00:00+05:00"
