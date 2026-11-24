"""The Django admin path of `professionals`: every row of the six editable models is created, changed and
deleted through :func:`save_row` / :func:`delete_row` (validated against the model invariants and audited as
``<snake_model>.created|updated|deleted``), plus the named helpers other code — seeders, M2 — uses."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.db.models.deletion import ProtectedError
from django.utils.translation import gettext, gettext_lazy, ngettext
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.core import audit, timeutils
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import media_name
from glowderma.core.uzsource import N_
from glowderma.core.variants import store_variants
from glowderma.professionals import bookings, selectors
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)
from glowderma.professionals.services import common, locations, specialists

SPECIALIST_LINK_ERROR = N_("Mutaxassis profili o‘z hisobiga butun umri davomida bog‘langan.")


# Audit entity names (contract §3.2); the override is audited the same way (design 03 open point S-3 / T13).
ENTITY_NAMES: dict[type[models.Model], str] = {
    Location: "location",
    Professional: "professional",
    Service: "service",
    WorkingHours: "working_hours",
    BlockedPeriod: "blocked_period",
    WorkingHoursOverride: "working_hours_override",
}


EDITABLE_MODELS: tuple[type[models.Model], ...] = tuple(ENTITY_NAMES)


# Refusals of the ops console's editable pages (the Django-admin path below): shown in the viewer's language.
IMAGE_MESSAGES = {
    "file_too_large": gettext_lazy("Rasm 5 MB dan oshmasin."),
    "unsupported_image": gettext_lazy("Faqat JPEG, PNG yoki WebP rasm."),
}


PROTECTED_MESSAGE = gettext_lazy(
    "Bu yozuvga boshqa yozuvlar bog‘langan; avval ularni o‘chiring yoki nofaol qiling."
)


def entity_name(model: type[models.Model]) -> str:
    try:
        return ENTITY_NAMES[model]
    except KeyError as exc:
        raise ValueError(f"{model.__name__} is not a professionals model with a service") from exc


def plain_value(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, models.fields.files.FieldFile):
        return value.name or ""
    return value


def snapshot(obj: models.Model) -> dict[str, Any]:
    """The audited image of a row: every concrete column, foreign keys by id, dates/times as ISO text."""
    return {field.attname: plain_value(getattr(obj, field.attname)) for field in obj._meta.concrete_fields}


def validate_row(obj: models.Model) -> None:
    """Model invariants (01 §5: weekday uniqueness, start < end, break inside the day, …) as form errors.

    ``full_clean`` minus the ``blank`` check on nullable columns left ``None`` (``experience_years``, a break,
    coordinates): the models declare them ``null=True`` without ``blank=True``, and ``None`` is their "not
    set" value. Every CHECK constraint is still validated (nothing is excluded from ``validate_constraints``).
    """
    unset_nullables = [
        field.name
        for field in obj._meta.concrete_fields
        if field.null and not field.blank and getattr(obj, field.attname) is None
    ]
    errors: dict[str, list[ValidationError]] = {}
    checks: tuple[Callable[[], None], ...] = (
        lambda: obj.clean_fields(exclude=unset_nullables),
        obj.clean,
        lambda: obj.validate_unique(exclude=unset_nullables),
        obj.validate_constraints,
    )
    for check in checks:
        try:
            check()
        except ValidationError as exc:
            errors = exc.update_error_dict(errors)
    if errors:
        raise ValidationError(errors)


def store_photo(professional: Professional, upload: Any) -> None:
    """Process (``core.images``), name and save ``upload`` as the professional's photo (the admin upload path
    and the demo seed)."""
    store_photo_file(professional, upload)


def store_photo_file(professional: Professional, upload: Any) -> None:
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ValidationError({"photo": IMAGE_MESSAGES[exc.code]}) from exc
    name = media_name(common.PHOTO_KIND, professional.pk, processed.content, processed.extension)
    professional.photo.save(name.rsplit("/", 1)[-1], ContentFile(processed.content), save=False)
    try:
        store_variants(professional.photo.storage, professional.photo.name or "", processed.content)
    except BaseException:
        professional.photo.storage.delete(professional.photo.name or "")  # the row rolls back
        raise
    professional.save(update_fields=["photo"])


def admin_upcoming_message(upcoming: int) -> str:
    return ngettext(
        "Mutaxassisda %(n)s ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Avval ularni boshqa "
        "vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling.",
        "Mutaxassisda %(n)s ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Avval ularni boshqa "
        "vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling.",
        upcoming,
    ) % {"n": upcoming}


def refuse_deactivation_with_upcoming(professional: Professional) -> None:
    """The PS-2 guard for the Django admin (and the named helpers): there is no ``force`` here, so a
    specialist with ``requested`` / ``confirmed`` appointments ahead is never deactivated outside the admin
    REST API (``PATCH specialists/{id}/`` with ``force: true``)."""
    upcoming = bookings.upcoming_counts([professional.pk], timeutils.now())[professional.pk]
    if upcoming:
        raise ValidationError({"is_active": admin_upcoming_message(upcoming)})


@transaction.atomic
def save_row(obj: models.Model, *, actor: Any, request: Any = None) -> models.Model:
    """Validate and persist ``obj`` (insert or update), then audit ``<entity>.created|updated``.

    Raises ``django.core.exceptions.ValidationError`` (what a ModelForm understands) for an invariant breach.
    """
    name = entity_name(type(obj))
    model = type(obj)
    before: dict[str, Any] | None = None
    if obj.pk is not None:
        rows = model._default_manager.filter(pk=obj.pk)
        if isinstance(obj, (Professional, Location)):
            rows = rows.select_for_update()  # serialises with bookings like ``update_specialist`` (PS-2)
        current = rows.first()
        before = snapshot(current) if current is not None else None
    if isinstance(obj, Professional) and (before is None or before["staff_user_id"] != obj.staff_user_id):
        raise ValidationError({"staff_user": gettext_lazy(SPECIALIST_LINK_ERROR)})
    if isinstance(obj, Professional) and before is not None and before["is_active"] and not obj.is_active:
        refuse_deactivation_with_upcoming(obj)
    if isinstance(obj, Professional) and obj.is_active and not (before and before["is_active"]):
        missing = selectors.publish_blockers(obj)
        if missing:
            raise ValidationError(
                {
                    "is_active": gettext(specialists.PUBLISH_BLOCKED)
                    % {
                        "missing": ", ".join(
                            gettext(specialists.PUBLISH_BLOCKER_MESSAGES[key]) for key in missing
                        )
                    }
                }
            )
    if isinstance(obj, Location) and before is not None and before["is_active"] and not obj.is_active:
        if locations.clinic_in_use(obj.pk):
            raise ValidationError({"is_active": gettext_lazy(common.API_MESSAGES["clinic_in_use"])})
    if isinstance(obj, Professional) and obj.location_id is not None:
        existing = (
            {before["location_id"], *obj.additional_locations.values_list("pk", flat=True)}
            if before
            else set()
        )
        target = {obj.location_id, *(existing - {before["location_id"]} if before else ())}
        specialists.check_clinic_assignments(obj, target, existing, form=True)
    if isinstance(obj, Service) and obj.professional_id is not None:
        Professional.objects.select_for_update().get(pk=obj.professional_id)  # specialist → service order
    if isinstance(obj, (WorkingHours, WorkingHoursOverride)) and obj.professional_id is not None:
        professional = Professional.objects.select_for_update().get(pk=obj.professional_id)
        if obj.location_id is not None:
            try:
                common.schedule_location_id(professional, obj.location_id)
            except ApiValidationError as exc:
                raise ValidationError(
                    {"location": gettext_lazy(common.API_MESSAGES["clinic_not_assigned"])}
                ) from exc
    pending_photo = None
    if isinstance(obj, Professional) and obj.photo and not getattr(obj.photo, "_committed", True):
        # The content-addressed name needs the row id: validate the bytes now, store them after the insert.
        pending_photo = obj.photo.file
        obj.photo = None if before is None else before.get("photo") or None
    validate_row(obj)
    obj.save()
    if isinstance(obj, Professional) and obj.location_id is not None:
        obj.additional_locations.remove(obj.location_id)
    if pending_photo is not None:
        store_photo_file(obj, pending_photo)  # type: ignore[arg-type]
    audit.record(
        action=f"{name}.{'updated' if before is not None else 'created'}",
        entity_type=name,
        entity_id=str(obj.pk),
        actor=actor,
        request=request,
        before=before,
        after=snapshot(obj),
    )
    return obj


@transaction.atomic
def delete_row(obj: models.Model, *, actor: Any, request: Any = None) -> None:
    """Delete ``obj`` and audit ``<entity>.deleted``; a PROTECT hit becomes a friendly ``ValidationError``."""
    name = entity_name(type(obj))
    before = snapshot(obj)
    pk = obj.pk
    if isinstance(obj, (WorkingHours, WorkingHoursOverride, Service)):
        Professional.objects.select_for_update().get(pk=obj.professional_id)
    try:
        obj.delete()
    except ProtectedError as exc:
        raise ValidationError(PROTECTED_MESSAGE) from exc
    audit.record(
        action=f"{name}.deleted",
        entity_type=name,
        entity_id=str(pk),
        actor=actor,
        request=request,
        before=before,
    )


def update_professional(
    professional: Professional, *, actor: Any, request: Any = None, **fields: Any
) -> Professional:
    for key, value in fields.items():
        setattr(professional, key, value)
    save_row(professional, actor=actor, request=request)
    return professional


def deactivate_professional(professional: Professional, *, actor: Any, request: Any = None) -> Professional:
    """Hide a specialist from the schedule and booking without touching the booked rows (PROTECT)."""
    return update_professional(professional, actor=actor, request=request, is_active=False)


def set_working_hours(
    professional: Professional,
    weekday: int,
    start: dt.time | None,
    end: dt.time | None,
    *,
    break_start: dt.time | None = None,
    break_end: dt.time | None = None,
    actor: Any,
    request: Any = None,
) -> WorkingHours | None:
    """Upsert the weekly hours of ``weekday``; ``start`` / ``end`` ``None`` removes the row (not working)."""
    row = WorkingHours.objects.filter(professional=professional, weekday=weekday).first()
    if start is None or end is None:
        if row is not None:
            delete_row(row, actor=actor, request=request)
        return None
    if row is None:
        row = WorkingHours(
            professional=professional,
            weekday=weekday,
            location_id=common.schedule_location_id(professional, professional.location_id),
        )
    row.start, row.end, row.break_start, row.break_end = start, end, break_start, break_end
    save_row(row, actor=actor, request=request)
    return row


def add_override(
    professional: Professional,
    date: dt.date,
    start: dt.time | None,
    end: dt.time | None,
    *,
    note: str = "",
    actor: Any,
    request: Any = None,
) -> WorkingHoursOverride:
    """Date-specific hours (both ``None`` = day off); one override per professional and date (upsert)."""
    row = WorkingHoursOverride.objects.filter(professional=professional, date=date).first()
    if row is None:
        row = WorkingHoursOverride(professional=professional, date=date, created_by=actor)
    row.start, row.end, row.note = start, end, note
    row.location_id = (row.location_id or professional.location_id) if start is not None else None
    save_row(row, actor=actor, request=request)
    return row


def add_blocked_period(
    professional: Professional,
    start: dt.datetime,
    end: dt.datetime,
    kind: str,
    *,
    label: str = "",
    actor: Any,
    request: Any = None,
) -> BlockedPeriod:
    block = BlockedPeriod(
        professional=professional, start=start, end=end, kind=kind, label=label, created_by=actor
    )
    save_row(block, actor=actor, request=request)
    return block


def remove_blocked_period(block: BlockedPeriod, *, actor: Any, request: Any = None) -> None:
    delete_row(block, actor=actor, request=request)


__all__ = [
    "EDITABLE_MODELS",
    "ENTITY_NAMES",
    "IMAGE_MESSAGES",
    "PROTECTED_MESSAGE",
    "SPECIALIST_LINK_ERROR",
    "add_blocked_period",
    "add_override",
    "admin_upcoming_message",
    "deactivate_professional",
    "delete_row",
    "entity_name",
    "plain_value",
    "refuse_deactivation_with_upcoming",
    "remove_blocked_period",
    "save_row",
    "set_working_hours",
    "snapshot",
    "store_photo",
    "store_photo_file",
    "update_professional",
    "validate_row",
]
