"""The professional editor after the UX review: weekdays as a select of Uzbek day names, times without
seconds, no per-row caption, «Mutaxassisni o‘chirish» and the inline add link without a stray capital."""

import pytest
from django.test import Client
from django.urls import reverse

from glowderma.core.labels import WEEKDAYS
from glowderma.core.tests.test_console_i18n import visible_text
from glowderma.professionals.admin import WorkingHoursInline
from glowderma.professionals.models import WorkingHours
from glowderma.professionals.tests.factories import ProfessionalFactory, WorkingHoursFactory

pytestmark = pytest.mark.django_db


def test_weekdays_are_uzbek_day_names() -> None:
    assert WorkingHours._meta.get_field("weekday").choices == list(enumerate(WEEKDAYS))
    assert WorkingHoursInline.show_title is False


def test_the_professional_page_reads_naturally(ops_client: Client) -> None:
    professional = ProfessionalFactory.create()
    WorkingHoursFactory.create(professional=professional, weekday=2)
    page = ops_client.get(reverse("admin:professionals_professional_change", args=[professional.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    text = visible_text(page.content)
    assert '<option value="2" selected>Chorshanba</option>' in html
    assert 'value="09:00"' in html and 'value="09:00:00"' not in html
    assert "Mutaxassisni o‘chirish" in text and "O‘chirish mutaxassis" not in text
    assert "Qo‘shish: Ish vaqti" in html and "Boshqa Ish vaqti" not in html
