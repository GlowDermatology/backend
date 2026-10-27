"""Design 03 §8: the appointments models are registered read-only with the design columns."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.appointments.tests.factories import AppointmentEventFactory, AppointmentFactory
from glowderma.core.admin import ReadOnlyAdmin

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("model", [Appointment, AppointmentEvent])
def test_registered_read_only(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    assert model_admin.list_display != ("__str__",) and model_admin.search_fields
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.check() == []


def test_design_columns() -> None:
    assert admin.site._registry[Appointment].list_display == (
        "id",
        "professional",
        "customer",
        "start",
        "status",
        "requested_via",
        "price",
    )
    assert admin.site._registry[Appointment].list_filter == ("status", "professional", "requested_via")
    assert admin.site._registry[AppointmentEvent].list_filter == ("type", "source")


def test_changelists_change_pages_and_search_render() -> None:
    client = Client()
    client.force_login(StaffUserFactory.create(is_staff=True, is_superuser=True))
    event = AppointmentEventFactory.create()
    other = AppointmentFactory.create()
    for row in (event.appointment, event, other):
        meta = row._meta
        assert client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")).status_code == 200
        change = client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[row.pk]))
        assert change.status_code == 200
        assert client.post(reverse(f"admin:{meta.app_label}_{meta.model_name}_add"), {}).status_code == 403
    phone = event.appointment.customer.phone
    search = client.get(reverse("admin:appointments_appointment_changelist"), {"q": phone})
    assert search.status_code == 200 and phone.encode() in search.content
    by_id = client.get(
        reverse("admin:appointments_appointmentevent_changelist"), {"q": str(event.appointment_id)}
    )
    assert by_id.status_code == 200 and b"requested" in by_id.content
