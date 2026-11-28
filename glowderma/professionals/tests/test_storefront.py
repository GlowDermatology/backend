"""``professionals.storefront``: a specialist, a service, a clinic, the hours, the clinics a specialist works
at, the photo and the landing's picks each mark the website's cache tags stale."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.files.base import ContentFile

from glowderma.accounts.models import StaffUser
from glowderma.catalog.tests.factories import image_bytes
from glowderma.core.tests.storefront_support import revalidated
from glowderma.professionals import services
from glowderma.professionals.models import Professional
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    LocationFactory,
    ProfessionalFactory,
    ServiceFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db
SPECIALISTS = {"shop:specialists", "shop:services"}
SCHEDULE = {"shop:availability", "shop:clinics"}


def test_specialists_services_and_clinics(django_capture_on_commit_callbacks: Any) -> None:
    capture = django_capture_on_commit_callbacks
    specialist = ProfessionalFactory.create()

    def rename() -> None:
        specialist.name = "Dilnoza Karimova"
        specialist.save(update_fields=["name"])

    assert revalidated(capture, rename) == SPECIALISTS
    assert revalidated(capture, lambda: ServiceFactory.create(professional=specialist)) == SPECIALISTS
    location = LocationFactory.create()
    assert revalidated(capture, lambda: LocationFactory.create()) == {"shop:clinics", "shop:specialists"}
    tags = revalidated(capture, lambda: specialist.additional_locations.add(location))
    assert tags == {"shop:specialists", "shop:clinics"}
    tags = revalidated(capture, lambda: specialist.additional_locations.remove(location))
    assert tags == {"shop:specialists", "shop:clinics"}


@pytest.mark.parametrize("factory", [WorkingHoursFactory, WorkingHoursOverrideFactory, BlockedPeriodFactory])
def test_the_hours_change_the_free_slots_and_the_clinics(
    django_capture_on_commit_callbacks: Any, factory: Any
) -> None:
    specialist = ProfessionalFactory.create()
    assert (
        revalidated(django_capture_on_commit_callbacks, lambda: factory.create(professional=specialist))
        >= SCHEDULE
    )


def test_removing_the_photo(django_capture_on_commit_callbacks: Any, staff: StaffUser) -> None:
    specialist = ProfessionalFactory.create()
    specialist.photo.save("photo.webp", ContentFile(image_bytes()), save=True)
    tags = revalidated(
        django_capture_on_commit_callbacks,
        lambda: services.delete_specialist_photo(specialist.pk, actor=staff),
    )
    assert "shop:specialists" in tags
    assert not Professional.objects.get(pk=specialist.pk).photo


def test_the_landing_picks(django_capture_on_commit_callbacks: Any, staff: StaffUser) -> None:
    capture = django_capture_on_commit_callbacks
    specialist = ProfessionalFactory.create()
    assert revalidated(capture, lambda: services.set_home_specialists([str(specialist.pk)], actor=staff)) == {
        "shop:home"
    }
    assert revalidated(capture, lambda: services.set_home_specialists([], actor=staff)) == {"shop:home"}
