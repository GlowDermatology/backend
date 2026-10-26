"""Design 01 §11 / 03 §8 + T13: seven professionals models are editable, every write through the services."""

from datetime import time

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.models import AuditEntry
from glowderma.professionals.admin import ServiceBackedAdmin
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
    Service,
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
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db

EDITABLE = [
    Location,
    Professional,
    Service,
    ProfessionalService,
    WorkingHours,
    WorkingHoursOverride,
    BlockedPeriod,
]


@pytest.mark.parametrize("model", EDITABLE)
def test_editable_models_use_the_service_backed_admin(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ServiceBackedAdmin)
    assert model_admin.list_display != ("__str__",) and model_admin.search_fields
    assert model_admin.actions is None  # bulk delete would bypass delete_model
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert model_admin.has_view_permission(request)
    assert model_admin.has_add_permission(request)
    assert model_admin.has_change_permission(request)
    assert model_admin.has_delete_permission(request)
    assert model_admin.check() == []
    request.user = StaffUserFactory.create(is_staff=False)
    assert not model_admin.has_change_permission(request)


def test_override_is_editable_through_the_services() -> None:
    model_admin = admin.site._registry[WorkingHoursOverride]
    assert isinstance(model_admin, ServiceBackedAdmin)
    assert model_admin.list_display == ("id", "professional", "date", "start", "end", "created_by")
    assert admin.site._registry[Professional].list_filter == ("role", "is_active")


def staff_client() -> Client:
    client = Client()
    client.force_login(StaffUserFactory.create(is_staff=True))
    return client


def test_changelists_and_change_pages_render() -> None:
    client = staff_client()
    link = ProfessionalServiceFactory.create()
    rows = [
        link.professional.location,
        link.professional,
        link.service,
        link,
        WorkingHoursFactory.create(professional=link.professional),
        WorkingHoursOverrideFactory.create(professional=link.professional),
        BlockedPeriodFactory.create(professional=link.professional),
        LocationFactory.create(),
        ServiceFactory.create(),
        ProfessionalFactory.create(),
    ]
    for row in rows:
        meta = row._meta
        assert client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")).status_code == 200
        change = client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[row.pk]))
        assert change.status_code == 200
    # T13: the override is editable; an empty add form is re-rendered with errors, not refused.
    assert client.post(reverse("admin:professionals_workinghoursoverride_add"), {}).status_code == 200
    search = client.get(reverse("admin:professionals_professional_changelist"), {"q": link.professional.name})
    assert search.status_code == 200 and link.professional.name.encode() in search.content


def test_add_change_and_delete_go_through_the_services() -> None:
    client = staff_client()
    response = client.post(
        reverse("admin:professionals_service_add"),
        {
            "name_uz": "Lazer epilyatsiya",
            "name_ru": "",
            "name_en": "",
            "short_name_uz": "Lazer",
            "short_name_ru": "",
            "short_name_en": "",
            "description_uz": "",
            "description_ru": "",
            "description_en": "",
            "category": "cosmetology",
            "default_duration_min": 40,
            "default_price": 500000,
            "position": 7,
            "is_active": "on",
        },
    )
    assert response.status_code == 302, response.content.decode()[:500]
    service = Service.objects.get(name_uz="Lazer epilyatsiya")
    created = AuditEntry.objects.get(action="service.created")
    assert created.entity_id == str(service.pk) and created.actor_type == "staff" and created.ip
    change_url = reverse("admin:professionals_service_change", args=[service.pk])
    data = {
        "name_uz": "Lazer epilyatsiya",
        "name_ru": "",
        "name_en": "",
        "short_name_uz": "Lazer",
        "short_name_ru": "",
        "short_name_en": "",
        "description_uz": "",
        "description_ru": "",
        "description_en": "",
        "category": "cosmetology",
        "default_duration_min": 50,
        "default_price": 500000,
        "position": 7,
        "is_active": "on",
    }
    assert client.post(change_url, data).status_code == 302
    updated = AuditEntry.objects.get(action="service.updated")
    assert updated.before is not None and updated.after is not None
    assert updated.before["default_duration_min"] == 40 and updated.after["default_duration_min"] == 50
    # an invariant breach is a form error, never a 500 or a silent write
    bad = client.post(change_url, {**data, "default_duration_min": 2})
    assert bad.status_code == 200 and Service.objects.get(pk=service.pk).default_duration_min == 50
    assert (
        client.post(
            reverse("admin:professionals_service_delete", args=[service.pk]), {"post": "yes"}
        ).status_code
        == 302
    )
    assert not Service.objects.filter(pk=service.pk).exists()
    deleted = AuditEntry.objects.get(action="service.deleted")
    assert deleted.before is not None and deleted.before["name_uz"] == "Lazer epilyatsiya"


def test_delete_of_a_referenced_service_is_refused_by_the_admin() -> None:
    client = staff_client()
    link = ProfessionalServiceFactory.create()
    response = client.post(
        reverse("admin:professionals_service_delete", args=[link.service.pk]), {"post": "yes"}
    )
    assert (
        response.status_code == 200
    )  # Django's "cannot delete" page: the PROTECT is explained, nothing changes
    assert Service.objects.filter(pk=link.service.pk).exists()
    assert not AuditEntry.objects.filter(action="service.deleted").exists()


def test_working_hours_inline_is_saved_through_the_services() -> None:
    client = staff_client()
    professional = ProfessionalFactory.create()
    existing = WorkingHoursFactory.create(professional=professional, weekday=0)
    data = {
        "location": professional.location_id,
        "name": professional.name,
        "role": professional.role,
        "bio_uz": "",
        "bio_ru": "",
        "bio_en": "",
        "experience_years": "",
        "slot_step_min": 30,
        "position": 0,
        "is_active": "on",
        "working_hours-TOTAL_FORMS": 2,
        "working_hours-INITIAL_FORMS": 1,
        "working_hours-MIN_NUM_FORMS": 0,
        "working_hours-MAX_NUM_FORMS": 1000,
        "working_hours-0-id": existing.pk,
        "working_hours-0-professional": professional.pk,
        "working_hours-0-weekday": 0,
        "working_hours-0-start": "10:00",
        "working_hours-0-end": "17:00",
        "working_hours-0-break_start": "",
        "working_hours-0-break_end": "",
        "working_hours-1-id": "",
        "working_hours-1-professional": professional.pk,
        "working_hours-1-weekday": 1,
        "working_hours-1-start": "09:00",
        "working_hours-1-end": "18:00",
        "working_hours-1-break_start": "13:00",
        "working_hours-1-break_end": "14:00",
    }
    response = client.post(reverse("admin:professionals_professional_change", args=[professional.pk]), data)
    assert response.status_code == 302, response.content.decode()[:800]
    assert WorkingHours.objects.get(pk=existing.pk).start == time(10, 0)
    tuesday = WorkingHours.objects.get(professional=professional, weekday=1)
    assert tuesday.break_start == time(13, 0)
    assert set(AuditEntry.objects.values_list("action", flat=True)) == {
        "professional.updated",
        "working_hours.updated",
        "working_hours.created",
    }
    data.update(
        {"working_hours-TOTAL_FORMS": 1, "working_hours-INITIAL_FORMS": 1, "working_hours-0-DELETE": "on"}
    )
    assert (
        client.post(
            reverse("admin:professionals_professional_change", args=[professional.pk]), data
        ).status_code
        == 302
    )
    assert not WorkingHours.objects.filter(pk=existing.pk).exists()
    assert AuditEntry.objects.filter(action="working_hours.deleted").exists()


def test_blocked_period_add_sets_created_by() -> None:
    client = staff_client()
    professional = ProfessionalFactory.create()
    response = client.post(
        reverse("admin:professionals_blockedperiod_add"),
        {
            "professional": professional.pk,
            "start_0": "2026-10-26",
            "start_1": "16:00:00",
            "end_0": "2026-10-26",
            "end_1": "16:30:00",
            "kind": "busy",
            "label": "Band qilingan",
        },
    )
    assert response.status_code == 302, response.content.decode()[:800]
    block = BlockedPeriod.objects.get(professional=professional)
    assert block.created_by is not None and block.created_by.is_staff
    created = AuditEntry.objects.get(action="blocked_period.created")
    assert created.after is not None and created.after["label"] == "Band qilingan"


# --- service refusals (fix pass): never a success banner for a row that was not saved ----------------------


def professional_form(location_id: int, **extra: object) -> dict[str, object]:
    return {
        "location": location_id,
        "name": "Probe Doctor",
        "role": "dermatologist",
        "bio_uz": "",
        "bio_ru": "",
        "bio_en": "",
        "experience_years": "",
        "slot_step_min": 30,
        "position": 0,
        "is_active": "on",
        "working_hours-TOTAL_FORMS": 0,
        "working_hours-INITIAL_FORMS": 0,
        "working_hours-MIN_NUM_FORMS": 0,
        "working_hours-MAX_NUM_FORMS": 1000,
        **extra,
    }


def flashed(response: object) -> list[tuple[str, str]]:
    from django.contrib.messages import get_messages

    return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]  # type: ignore[attr-defined]


def test_an_oversized_photo_re_renders_the_add_form_without_a_success_message() -> None:
    import io
    import os

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    noise = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    assert buffer.tell() > 5 * 1024 * 1024  # passes the ModelForm's ImageField, fails process_image
    upload = SimpleUploadedFile("big.png", buffer.getvalue(), content_type="image/png")
    client = staff_client()
    location = LocationFactory.create()
    response = client.post(
        reverse("admin:professionals_professional_add"), professional_form(location.pk, photo=upload)
    )
    assert response.status_code == 200  # the bound form again, not a redirect
    assert response.context["adminform"].form.errors["photo"] == ["Rasm 5 MB dan oshmasin."]
    assert "Probe Doctor" in response.content.decode()  # the input is kept
    assert flashed(response) == []  # no "was added successfully"
    assert not Professional.objects.exists()
    assert not AuditEntry.objects.filter(action="professional.created").exists()
    from django.contrib.admin.models import LogEntry

    assert not LogEntry.objects.exists()


def test_a_refused_save_rolls_back_and_shows_the_errors_on_the_form(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.core.exceptions import ValidationError

    from glowderma.professionals import services

    def refuse(obj: models.Model, **kwargs: object) -> models.Model:
        obj.save()  # a write that must not survive the refusal
        raise ValidationError({"name_uz": "Band.", "nonexistent": "Boshqa sabab."})

    monkeypatch.setattr(services, "save_row", refuse)
    client = staff_client()
    response = client.post(
        reverse("admin:professionals_service_add"),
        {
            "name_uz": "Yangi",
            "short_name_uz": "Yangi",
            "category": "dermatology",
            "default_duration_min": 30,
            "default_price": 100000,
            "position": 1,
            "is_active": "on",
        },
    )
    assert response.status_code == 200
    form = response.context["adminform"].form
    assert form.errors["name_uz"] == ["Band."]
    assert form.non_field_errors() == ["Boshqa sabab."]
    assert flashed(response) == []
    assert not Service.objects.exists()


def test_a_refused_inline_row_rolls_back_the_whole_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.core.exceptions import ValidationError

    from glowderma.professionals import services

    professional = ProfessionalFactory.create(name="Asl nom")
    real_save_row = services.save_row

    def refuse_hours(obj: models.Model, **kwargs: object) -> models.Model:
        if isinstance(obj, WorkingHours):
            raise ValidationError("Ish vaqti kesishadi.")
        return real_save_row(obj, **kwargs)

    monkeypatch.setattr(services, "save_row", refuse_hours)
    data = professional_form(
        professional.location_id,
        name="Yangi nom",
        **{
            "working_hours-TOTAL_FORMS": 1,
            "working_hours-0-id": "",
            "working_hours-0-professional": professional.pk,
            "working_hours-0-weekday": 2,
            "working_hours-0-start": "09:00",
            "working_hours-0-end": "18:00",
            "working_hours-0-break_start": "",
            "working_hours-0-break_end": "",
        },
    )
    data["role"] = professional.role
    response = staff_client().post(
        reverse("admin:professionals_professional_change", args=[professional.pk]), data
    )
    assert response.status_code == 200
    assert response.context["adminform"].form.non_field_errors() == ["Ish vaqti kesishadi."]
    assert flashed(response) == []
    assert Professional.objects.get(pk=professional.pk).name == "Asl nom"  # the parent save rolled back
    assert not WorkingHours.objects.filter(professional=professional).exists()
    assert not AuditEntry.objects.exists()


def test_a_refused_delete_redirects_to_the_change_page_with_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.core.exceptions import ValidationError

    from glowderma.professionals import services

    def refuse(obj: models.Model, **kwargs: object) -> None:
        obj.delete()
        raise ValidationError(services.PROTECTED_MESSAGE)

    monkeypatch.setattr(services, "delete_row", refuse)
    service = ServiceFactory.create()
    response = staff_client().post(
        reverse("admin:professionals_service_delete", args=[service.pk]), {"post": "yes"}
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("admin:professionals_service_change", args=[service.pk])
    assert flashed(response) == [("error", services.PROTECTED_MESSAGE)]
    assert Service.objects.filter(pk=service.pk).exists()


def test_override_add_and_delete_are_audited() -> None:
    from datetime import date, time

    from glowderma.core.models import AuditEntry

    client = staff_client()
    professional = ProfessionalFactory.create()
    response = client.post(
        reverse("admin:professionals_workinghoursoverride_add"),
        {"professional": professional.pk, "date": "2026-11-02", "start": "10:00", "end": "15:00", "note": ""},
    )
    assert response.status_code == 302, response.content
    row = WorkingHoursOverride.objects.get(professional=professional, date=date(2026, 11, 2))
    assert (row.start, row.end) == (time(10), time(15)) and row.created_by is not None
    assert AuditEntry.objects.filter(action="working_hours_override.created", entity_id=str(row.pk)).exists()
    response = client.post(
        reverse("admin:professionals_workinghoursoverride_delete", args=[row.pk]), {"post": "yes"}
    )
    assert response.status_code == 302
    assert not WorkingHoursOverride.objects.filter(pk=row.pk).exists()
    assert AuditEntry.objects.filter(action="working_hours_override.deleted", entity_id=str(row.pk)).exists()
