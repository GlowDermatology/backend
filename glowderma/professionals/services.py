"""Writes of `professionals` (design 01 §5, 03 §8): every row of the six editable models — and the date
override — is created, changed and deleted here, validated against the model invariants and audited as
``<snake_model>.created|updated|deleted`` (contract §3.2 "Django admin (editable models)").

The Django admin (:mod:`glowderma.professionals.admin`) routes ``save_model`` / ``delete_model`` / inline
formsets through :func:`save_row` and :func:`delete_row`; the named helpers below are the service surface
other code (seeders, M2, future endpoints) uses. There is no admin REST API for these models in M1.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.db.models.deletion import ProtectedError

from glowderma.core import audit
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import media_name
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)

PHOTO_KIND = "professionals"
# Audit entity names (contract §3.2); the override is audited the same way (design 03 open point S-3 / T13).
ENTITY_NAMES: dict[type[models.Model], str] = {
    Location: "location",
    Professional: "professional",
    Service: "service",
    ProfessionalService: "professional_service",
    WorkingHours: "working_hours",
    BlockedPeriod: "blocked_period",
    WorkingHoursOverride: "working_hours_override",
}
EDITABLE_MODELS: tuple[type[models.Model], ...] = tuple(ENTITY_NAMES)
IMAGE_MESSAGES = {
    "file_too_large": "Rasm 5 MB dan oshmasin.",
    "unsupported_image": "Faqat JPEG, PNG yoki WebP rasm.",
}
PROTECTED_MESSAGE = "Bu yozuvga boshqa yozuvlar bog‘langan; avval ularni o‘chiring yoki nofaol qiling."


def entity_name(model: type[models.Model]) -> str:
    try:
        return ENTITY_NAMES[model]
    except KeyError as exc:
        raise ValueError(f"{model.__name__} is not a professionals model with a service") from exc


def _plain(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, models.fields.files.FieldFile):
        return value.name or ""
    return value


def snapshot(obj: models.Model) -> dict[str, Any]:
    """The audited image of a row: every concrete column, foreign keys by id, dates/times as ISO text."""
    return {field.attname: _plain(getattr(obj, field.attname)) for field in obj._meta.concrete_fields}


def _validate(obj: models.Model) -> None:
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


def _store_photo(professional: Professional, upload: Any) -> None:
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ValidationError({"photo": IMAGE_MESSAGES[exc.code]}) from exc
    name = media_name(PHOTO_KIND, professional.pk, processed.content, processed.extension)
    professional.photo.save(name.rsplit("/", 1)[-1], ContentFile(processed.content), save=False)
    professional.save(update_fields=["photo"])


@transaction.atomic
def save_row(obj: models.Model, *, actor: Any, request: Any = None) -> models.Model:
    """Validate and persist ``obj`` (insert or update), then audit ``<entity>.created|updated``.

    Raises ``django.core.exceptions.ValidationError`` (what a ModelForm understands) for an invariant breach.
    """
    name = entity_name(type(obj))
    model = type(obj)
    before: dict[str, Any] | None = None
    if obj.pk is not None:
        current = model._default_manager.filter(pk=obj.pk).first()
        before = snapshot(current) if current is not None else None
    pending_photo = None
    if isinstance(obj, Professional) and obj.photo and not getattr(obj.photo, "_committed", True):
        # The content-addressed name needs the row id: validate the bytes now, store them after the insert.
        pending_photo = obj.photo.file
        obj.photo = None if before is None else before.get("photo") or None
    _validate(obj)
    obj.save()
    if pending_photo is not None:
        _store_photo(obj, pending_photo)  # type: ignore[arg-type]
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


# --- named helpers ----------------------------------------------------------------------------------------


def create_professional(
    *, location: Location, name: str, role: str, actor: Any, request: Any = None, **fields: Any
) -> Professional:
    professional = Professional(location=location, name=name, role=role, **fields)
    save_row(professional, actor=actor, request=request)
    return professional


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
        row = WorkingHours(professional=professional, weekday=weekday)
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


def link_service(
    professional: Professional,
    service: Service,
    *,
    price: int,
    duration_min: int | None = None,
    is_active: bool = True,
    actor: Any,
    request: Any = None,
) -> ProfessionalService:
    """Make ``professional`` perform ``service`` at ``price`` / ``duration_min`` (upsert of the link)."""
    link = ProfessionalService.objects.filter(professional=professional, service=service).first()
    if link is None:
        link = ProfessionalService(professional=professional, service=service)
    link.price = price
    link.duration_min = service.default_duration_min if duration_min is None else duration_min
    link.is_active = is_active
    save_row(link, actor=actor, request=request)
    return link


def unlink_service(link: ProfessionalService, *, actor: Any, request: Any = None) -> None:
    delete_row(link, actor=actor, request=request)
