"""Design 01 §11 / 03 §8: the professionals models are read-only in this slice (editing comes with B6)."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.admin import ReadOnlyAdmin
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

MODELS = [
    Location,
    Professional,
    Service,
    ProfessionalService,
    WorkingHours,
    WorkingHoursOverride,
    BlockedPeriod,
]


@pytest.mark.parametrize("model", MODELS)
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


def test_override_admin_lists_the_design_columns() -> None:
    assert admin.site._registry[WorkingHoursOverride].list_display == (
        "id",
        "professional",
        "date",
        "start",
        "end",
        "created_by",
    )
    assert admin.site._registry[Professional].list_filter == ("role", "is_active")


def test_changelists_and_change_pages_render() -> None:
    client = Client()
    client.force_login(StaffUserFactory.create(is_staff=True, is_superuser=True))
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
        assert client.post(reverse(f"admin:{meta.app_label}_{meta.model_name}_add"), {}).status_code == 403
    search = client.get(reverse("admin:professionals_professional_changelist"), {"q": link.professional.name})
    assert search.status_code == 200 and link.professional.name.encode() in search.content
