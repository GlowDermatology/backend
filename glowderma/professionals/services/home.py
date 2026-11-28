"""The landing's «Mutaxassisga yoziling» picks (admin «Bosh sahifa»): which specialists, in which order. With
none, the landing lists every specialist ``specialists/`` lists, as it always did."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.db import transaction
from rest_framework.exceptions import ValidationError

from glowderma.core import audit, enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.uzsource import N_
from glowderma.professionals import selectors
from glowderma.professionals.models import HOME_SPECIALISTS_LIMIT, HomeSpecialist, Professional
from glowderma.professionals.services import common

HOME_SPECIALISTS_LOCK = "professionals.home_specialists"
HOME_SPECIALIST_HIDDEN = N_("«%(name)s» saytda ko‘rinmaydi: profil e’lon qilinmagan yoki filiali faol emas.")


def set_home_specialists(refs: Sequence[str], *, actor: Any, request: Any = None) -> list[str]:
    """Replaces the landing's specialists with ``refs`` (specialist ids) in that order; an empty list makes
    the section automatic again. Refuses a specialist customers could not see now. Audited as
    ``home_section.saved`` (``entity_id`` ``specialists``). Returns the saved refs."""
    if len(refs) > HOME_SPECIALISTS_LIMIT:
        raise ValidationError({"ids": [field_error("max_items", limit=HOME_SPECIALISTS_LIMIT)]})
    if len(set(refs)) != len(refs):
        raise ValidationError({"ids": [field_error("invalid", N_("Har bir element bir marta tanlansin."))]})
    ids = [int(ref) for ref in refs if ref.isdigit()]
    names = dict(Professional.objects.filter(pk__in=ids).values_list("pk", "name"))
    if len(ids) != len(refs) or set(ids) - set(names):
        raise ValidationError({"ids": [field_error("does_not_exist")]})
    issues = selectors.specialist_pick_issues(ids)
    problems = [
        field_error("home_pick_unavailable", HOME_SPECIALIST_HIDDEN % {"name": names[pk]})
        for pk in ids
        if issues[pk] is not None
    ]
    if problems:
        raise ValidationError({"ids": problems})
    with transaction.atomic():
        common.advisory_lock(HOME_SPECIALISTS_LOCK)
        before = [str(pk) for pk in selectors.home_specialist_ids()]
        HomeSpecialist.objects.all().delete()
        HomeSpecialist.objects.bulk_create(
            HomeSpecialist(professional_id=pk, position=position) for position, pk in enumerate(ids)
        )
        audit.record(
            action="home_section.saved",
            entity_type="home_section",
            entity_id=enums.HomeSectionKey.SPECIALISTS,
            actor=actor,
            request=request,
            before={"ids": before},
            after={"ids": list(refs)},
        )
    return list(refs)


__all__ = [
    "HOME_SPECIALIST_HIDDEN",
    "set_home_specialists",
]
