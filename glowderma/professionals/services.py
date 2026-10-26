"""Writes of `professionals` (design 01 §5, 03 §8): every row of the six editable models — and the date
override — is created, changed and deleted here, validated against the model invariants and audited as
``<snake_model>.created|updated|deleted`` (contract §3.2 "Django admin (editable models)").

The Django admin (:mod:`glowderma.professionals.admin`) routes ``save_model`` / ``delete_model`` / inline
formsets through :func:`save_row` and :func:`delete_row`; the named helpers below are the service surface
other code (seeders, M2) uses. The admin REST API (FND-1, ``api/admin/``) has its own operations at the end
of this module: they raise the contract's DRF errors and audit one row per operation (``professional.*``,
``service.*``, ``schedule.*``, ``location.*``, contract "Mutaxassislar va xizmatlar").
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from hashlib import sha256
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, models, transaction
from django.db.models.deletion import ProtectedError
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import Conflict, field_error
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import media_name
from glowderma.professionals import bookings, selectors
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


# --- admin API (FND-1): specialists, services, schedules and the location ----------------------------------
# Writes of ``api/admin/`` (contract "Mutaxassislar va xizmatlar"). The serializers validate shapes and
# value rules and resolve references; these functions own the state rules (uniqueness, overlaps, the
# deactivation guard), run under the specialist's row lock (``SELECT … FOR UPDATE``: concurrent edits of one
# specialist serialise, and a booking's ``FOR KEY SHARE`` of the specialist — ``appointments.services`` —
# waits for a deactivation or makes it wait) and write one audit row per operation, only when something
# changed. Existing appointments are never touched: they keep
# their time, price and duration snapshots whatever the schedule, the offered services or the prices become.

# The admin booking grid (``appointments.services.SLOT_STEP_MIN``; equal by a test, the layers forbid the
# import): durations and schedule times are multiples of it, so every booking engine can place them.
DURATION_STEP_MIN = 5
MAX_PRICE = 100_000_000
OVERRIDE_HORIZON_DAYS = 365
BLOCK_MAX_DAYS = 366
STAFF_USER_CONSTRAINT = "professionals_professional_staff_user_id_key"
OVERRIDE_CONSTRAINT = "hoursoverride_professional_date_unique"
LOCATION_LOCK = "professionals.location"
API_MESSAGES = {
    "location_missing": "Avval klinika manzilini kiriting.",
    "staff_taken": "Bu xodim boshqa mutaxassisga bog‘langan.",
    "has_upcoming": (
        "Mutaxassisda {n} ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Qabullar o‘zgarmaydi: avval "
        "ularni boshqa vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling, yoki baribir nofaol qiling."
    ),
    "override_taken": "Bu sana uchun alohida jadval allaqachon bor.",
    "block_overlap": "Bu vaqt boshqa band vaqt bilan kesishadi.",
    "latlng_pair": "Kenglik va uzunlikni birga kiriting.",
}


def _advisory_lock(key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


def _violates(exc: IntegrityError, constraint: str) -> bool:
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
    if name is not None:
        return bool(name == constraint)
    return constraint in str(exc)  # pragma: no cover - a driver without diagnostics


def _save_unique(save: Callable[[], None], constraint: str, field: str, message: str) -> None:
    """``save`` in a savepoint; losing the race on ``constraint`` becomes 400 ``unique`` on ``field``."""
    try:
        with transaction.atomic():
            save()
    except IntegrityError as exc:
        if not _violates(exc, constraint):
            raise
        raise ApiValidationError({field: [field_error("unique", message)]}) from exc


def _diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    changed = [key for key in after if before.get(key) != after[key]]
    return {key: before.get(key) for key in changed}, {key: after[key] for key in changed}


def _next_position(model: type[Professional] | type[Service]) -> int:
    last = model.objects.order_by("-position").values_list("position", flat=True).first()
    return 0 if last is None else int(last) + 1


def _clock(value: dt.time | None) -> str | None:
    return None if value is None else value.strftime("%H:%M")


def _locked_professional(pk: int) -> Professional:
    professional = Professional.objects.select_for_update().filter(pk=pk).first()
    if professional is None:
        raise NotFound
    return professional


def _i18n_state(obj: models.Model, prefix: str) -> dict[str, str]:
    return {f"{prefix}.{language}": getattr(obj, f"{prefix}_{language}") for language in ("uz", "ru", "en")}


def _set_i18n(obj: models.Model, prefix: str, texts: dict[str, str] | None) -> None:
    for language, value in (texts or {}).items():
        setattr(obj, f"{prefix}_{language}", value)


def _specialist_state(professional: Professional) -> dict[str, Any]:
    return {
        "name": professional.name,
        "role": professional.role,
        **_i18n_state(professional, "qualifications"),
        **_i18n_state(professional, "bio"),
        "experience_years": professional.experience_years,
        "slot_step_min": professional.slot_step_min,
        "location_id": professional.location_id,
        "staff_user_id": professional.staff_user_id,
        "position": professional.position,
        "is_active": professional.is_active,
    }


SPECIALIST_SCALARS = ("name", "role", "experience_years", "slot_step_min", "position", "is_active")


def _apply_specialist(professional: Professional, data: dict[str, Any]) -> None:
    for key in SPECIALIST_SCALARS:
        if key in data:
            setattr(professional, key, data[key])
    if "location" in data:
        professional.location = data["location"]
    if "staff_user" in data:
        professional.staff_user = data["staff_user"]
    _set_i18n(professional, "qualifications", data.get("qualifications"))
    _set_i18n(professional, "bio", data.get("bio"))


def create_specialist(data: dict[str, Any], *, actor: Any, request: Any = None) -> Professional:
    """``POST specialists/``: the location defaults to the clinic's (decision P4); appended last unless a
    ``position`` is given; audit ``professional.created``."""
    with transaction.atomic():
        location = data.get("location") or selectors.the_location()
        if location is None:
            raise ApiValidationError(
                {"location_id": [field_error("required", API_MESSAGES["location_missing"])]}
            )
        professional = Professional(location=location, position=_next_position(Professional))
        _apply_specialist(professional, {**data, "location": location})
        _save_unique(professional.save, STAFF_USER_CONSTRAINT, "staff_user_id", API_MESSAGES["staff_taken"])
        audit.record(
            action="professional.created",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            after=_specialist_state(professional),
        )
    return professional


def update_specialist(
    professional_id: int, data: dict[str, Any], *, force: bool = False, actor: Any, request: Any = None
) -> Professional:
    """``PATCH specialists/{id}/``. Deactivating a specialist with ``requested`` / ``confirmed`` appointments
    ahead is refused (409 ``conflict`` naming the count) unless ``force``; the appointments are kept either
    way. Audit ``professional.deactivated`` / ``professional.activated`` when ``is_active`` flips (with
    the other changed fields), else ``professional.updated``; nothing changed → no write, no audit."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        before = _specialist_state(professional)
        _apply_specialist(professional, data)
        after = _specialist_state(professional)
        if after == before:
            return professional
        extra: dict[str, Any] = {}
        action = "professional.updated"
        if before["is_active"] != after["is_active"]:
            action = "professional.activated" if after["is_active"] else "professional.deactivated"
        if action == "professional.deactivated":
            upcoming = bookings.upcoming_counts([professional.pk], timeutils.now())[professional.pk]
            if upcoming and not force:
                raise Conflict(API_MESSAGES["has_upcoming"].format(n=upcoming))
            extra = {"upcoming_appointments": upcoming, "forced": bool(upcoming)}
        _save_unique(professional.save, STAFF_USER_CONSTRAINT, "staff_user_id", API_MESSAGES["staff_taken"])
        diff_before, diff_after = _diff(before, after)
        audit.record(
            action=action,
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before=diff_before,
            after={**diff_after, **extra},
        )
    return professional


def set_specialist_photo(
    professional_id: int, upload: Any, *, actor: Any, request: Any = None
) -> Professional:
    """``PUT specialists/{id}/photo/``: re-encoded by ``core.images`` (the product-image limits), stored under
    a content-addressed name; the previous file is removed after commit. Audit ``professional.photo_updated``
    (the same bytes again change nothing)."""
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ApiValidationError({"file": [field_error(exc.code)]}) from exc
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        previous = professional.photo.name or ""
        name = media_name(PHOTO_KIND, professional.pk, processed.content, processed.extension)
        if previous == name:
            return professional
        professional.photo.save(name.rsplit("/", 1)[-1], ContentFile(processed.content), save=False)
        professional.save(update_fields=["photo"])
        if previous:
            _remove_file_on_commit(professional.photo.storage, previous)
        audit.record(
            action="professional.photo_updated",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before={"photo": previous or None},
            after={"photo": professional.photo.name, "sha256": sha256(processed.content).hexdigest()},
        )
    return professional


def delete_specialist_photo(professional_id: int, *, actor: Any, request: Any = None) -> None:
    """``DELETE specialists/{id}/photo/`` (idempotent): audit ``professional.photo_deleted`` when there was
    one; the file is removed after commit."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        previous = professional.photo.name or ""
        if not previous:
            return
        Professional.objects.filter(pk=professional.pk).update(photo="", updated_at=timeutils.now())
        _remove_file_on_commit(professional.photo.storage, previous)
        audit.record(
            action="professional.photo_deleted",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before={"photo": previous},
            after={"photo": None},
        )


def _remove_file_on_commit(storage: Any, name: str) -> None:
    transaction.on_commit(lambda: storage.delete(name))


def _links_state(professional: Professional) -> list[dict[str, Any]]:
    return [
        {
            "service_id": link.service_id,
            "price": link.price,
            "duration_min": link.duration_min,
            "is_active": link.is_active,
        }
        for link in ProfessionalService.objects.filter(professional=professional).order_by("service_id")
    ]


def set_offered_services(
    professional_id: int, items: list[dict[str, Any]], *, actor: Any, request: Any = None
) -> Professional:
    """``PUT specialists/{id}/services/``: the exact set of offered services (``service`` resolved by the
    serializer, ``price``, ``duration_min`` — default the service's — and ``is_active``). A link missing from
    the set is removed. Bookings already made keep their snapshots. Audit ``professional.services_updated``
    (the before / after sets) when the set changed."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        before = _links_state(professional)
        current = {
            link.service_id: link for link in ProfessionalService.objects.filter(professional=professional)
        }
        now = timeutils.now()
        for item in items:
            service: Service = item["service"]
            link = current.pop(service.pk, None) or ProfessionalService(
                professional=professional, service=service
            )
            values = {
                "price": item["price"],
                "duration_min": item.get("duration_min") or service.default_duration_min,
                "is_active": item.get("is_active", True),
            }
            if link.pk is not None and all(getattr(link, key) == value for key, value in values.items()):
                continue
            for key, value in values.items():
                setattr(link, key, value)
            link.updated_at = now
            link.save()
        ProfessionalService.objects.filter(pk__in=[link.pk for link in current.values()]).delete()
        after = _links_state(professional)
        if after != before:
            audit.record(
                action="professional.services_updated",
                entity_type="professional",
                entity_id=str(professional.pk),
                actor=actor,
                request=request,
                before={"services": before},
                after={"services": after},
            )
    return professional


def _week_state(professional: Professional) -> list[dict[str, Any]]:
    return [
        {
            "weekday": row.weekday,
            "start": _clock(row.start),
            "end": _clock(row.end),
            "break_start": _clock(row.break_start),
            "break_end": _clock(row.break_end),
        }
        for row in WorkingHours.objects.filter(professional=professional).order_by("weekday")
    ]


def set_working_week(
    professional_id: int, days: list[dict[str, Any]], *, actor: Any, request: Any = None
) -> Professional:
    """``PUT specialists/{id}/working-hours/``: the whole week (a weekday left out is not a working day).
    Appointments already booked outside the new hours are kept (``GET specialists/{id}/conflicts/`` lists
    them). Audit ``schedule.hours_updated`` (the before / after weeks) when the week changed."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        before = _week_state(professional)
        rows = {row.weekday: row for row in WorkingHours.objects.filter(professional=professional)}
        for day in days:
            row = rows.pop(day["weekday"], None) or WorkingHours(
                professional=professional, weekday=day["weekday"]
            )
            values = {key: day.get(key) for key in ("start", "end", "break_start", "break_end")}
            if row.pk is not None and all(getattr(row, key) == value for key, value in values.items()):
                continue
            for key, value in values.items():
                setattr(row, key, value)
            row.save()
        WorkingHours.objects.filter(pk__in=[row.pk for row in rows.values()]).delete()
        after = _week_state(professional)
        if after != before:
            audit.record(
                action="schedule.hours_updated",
                entity_type="professional",
                entity_id=str(professional.pk),
                actor=actor,
                request=request,
                before={"days": before},
                after={"days": after},
            )
    return professional


def _override_state(row: WorkingHoursOverride) -> dict[str, Any]:
    return {
        "override_id": row.pk,
        "date": row.date.isoformat(),
        "start": _clock(row.start),
        "end": _clock(row.end),
        "note": row.note,
    }


def create_override(
    professional_id: int, data: dict[str, Any], *, actor: Any, request: Any = None
) -> WorkingHoursOverride:
    """``POST specialists/{id}/overrides/``: one date's own hours (both ``None`` = day off); one per date
    (400 ``date`` ``unique``). Audit ``schedule.override_created``."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        if WorkingHoursOverride.objects.filter(professional=professional, date=data["date"]).exists():
            raise ApiValidationError({"date": [field_error("unique", API_MESSAGES["override_taken"])]})
        row = WorkingHoursOverride(
            professional=professional,
            date=data["date"],
            start=data.get("start"),
            end=data.get("end"),
            note=data.get("note", ""),
            created_by=actor,
        )
        _save_unique(row.save, OVERRIDE_CONSTRAINT, "date", API_MESSAGES["override_taken"])
        audit.record(
            action="schedule.override_created",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            after=_override_state(row),
        )
    return row


def delete_override(professional_id: int, override_id: int, *, actor: Any, request: Any = None) -> None:
    """``DELETE specialists/{id}/overrides/{override_id}/``: the date falls back to the weekly hours; 404 for
    another specialist's row. Audit ``schedule.override_deleted``."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        row = WorkingHoursOverride.objects.filter(professional=professional, pk=override_id).first()
        if row is None:
            raise NotFound
        state = _override_state(row)
        row.delete()
        audit.record(
            action="schedule.override_deleted",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before=state,
        )


def _block_state(block: BlockedPeriod) -> dict[str, Any]:
    return {
        "block_id": block.pk,
        "start": timeutils.isoformat(block.start),
        "end": timeutils.isoformat(block.end),
        "kind": block.kind,
        "label": block.label,
    }


def create_block(
    professional_id: int, data: dict[str, Any], *, actor: Any, request: Any = None
) -> BlockedPeriod:
    """``POST specialists/{id}/blocks/``: an absolute busy / off range that overlaps none of the
    specialist's other blocks (400 ``start`` ``overlap``). Appointments inside it are kept (they show up in
    ``conflicts/``). Audit ``schedule.block_created``."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        start, end = data["start"], data["end"]
        if BlockedPeriod.objects.filter(professional=professional, start__lt=end, end__gt=start).exists():
            raise ApiValidationError({"start": [field_error("overlap", API_MESSAGES["block_overlap"])]})
        block = BlockedPeriod.objects.create(
            professional=professional,
            start=start,
            end=end,
            kind=data["kind"],
            label=data.get("label") or "",
            created_by=actor,
        )
        audit.record(
            action="schedule.block_created",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            after=_block_state(block),
        )
    return block


def delete_block(professional_id: int, block_id: int, *, actor: Any, request: Any = None) -> None:
    """``DELETE specialists/{id}/blocks/{block_id}/``: 404 for another specialist's block. Audit
    ``schedule.block_deleted``."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        block = BlockedPeriod.objects.filter(professional=professional, pk=block_id).first()
        if block is None:
            raise NotFound
        state = _block_state(block)
        block.delete()
        audit.record(
            action="schedule.block_deleted",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before=state,
        )


def _service_state(service: Service) -> dict[str, Any]:
    return {
        **_i18n_state(service, "name"),
        **_i18n_state(service, "short_name"),
        **_i18n_state(service, "description"),
        "category": service.category,
        "default_duration_min": service.default_duration_min,
        "default_price": service.default_price,
        "position": service.position,
        "is_active": service.is_active,
    }


SERVICE_SCALARS = ("category", "default_duration_min", "default_price", "position", "is_active")


def _apply_service(service: Service, data: dict[str, Any]) -> None:
    for key in SERVICE_SCALARS:
        if key in data:
            setattr(service, key, data[key])
    for prefix in ("name", "short_name", "description"):
        _set_i18n(service, prefix, data.get(prefix))


def create_service(data: dict[str, Any], *, actor: Any, request: Any = None) -> Service:
    """``POST services/``: appended last unless a ``position`` is given; audit ``service.created``."""
    with transaction.atomic():
        service = Service(position=_next_position(Service))
        _apply_service(service, data)
        service.save()
        audit.record(
            action="service.created",
            entity_type="service",
            entity_id=str(service.pk),
            actor=actor,
            request=request,
            after=_service_state(service),
        )
    return service


def update_service(service_id: int, data: dict[str, Any], *, actor: Any, request: Any = None) -> Service:
    """``PATCH services/{id}/`` (no delete: ``is_active: false`` retires a service). A deactivated service
    leaves the customer catalogue and the booking dialogs at once; its appointments are kept. Audit
    ``service.deactivated`` / ``service.activated`` when ``is_active`` flips, else ``service.updated``."""
    with transaction.atomic():
        service = Service.objects.select_for_update().filter(pk=service_id).first()
        if service is None:
            raise NotFound
        before = _service_state(service)
        _apply_service(service, data)
        after = _service_state(service)
        if after == before:
            return service
        action = "service.updated"
        if before["is_active"] != after["is_active"]:
            action = "service.activated" if after["is_active"] else "service.deactivated"
        service.save()
        diff_before, diff_after = _diff(before, after)
        audit.record(
            action=action,
            entity_type="service",
            entity_id=str(service.pk),
            actor=actor,
            request=request,
            before=diff_before,
            after=diff_after,
        )
    return service


def _location_state(location: Location) -> dict[str, Any]:
    return {
        **_i18n_state(location, "name"),
        **_i18n_state(location, "address"),
        "phone": location.phone or None,
        "latitude": None if location.latitude is None else str(location.latitude),
        "longitude": None if location.longitude is None else str(location.longitude),
    }


def update_location(data: dict[str, Any], *, actor: Any, request: Any = None) -> Location:
    """``PATCH location/``: the clinic (decision P4), created by the first PATCH (``name.uz`` then
    required). Coordinates are a pair. Audit ``location.created`` / ``location.updated``."""
    with transaction.atomic():
        _advisory_lock(LOCATION_LOCK)  # two first PATCHes must not create two clinics
        location = Location.objects.select_for_update().order_by("position", "id").first()
        created = location is None
        if location is None:
            if not (data.get("name") or {}).get("uz"):
                raise ApiValidationError({"name.uz": [field_error("required")]})
            location = Location()
        before = {} if created else _location_state(location)
        _set_i18n(location, "name", data.get("name"))
        _set_i18n(location, "address", data.get("address"))
        if "phone" in data:
            location.phone = data["phone"] or ""
        for key in ("latitude", "longitude"):
            if key in data:
                setattr(location, key, data[key])
        if (location.latitude is None) != (location.longitude is None):
            missing = "latitude" if location.latitude is None else "longitude"
            raise ApiValidationError({missing: [field_error("latlng_pair", API_MESSAGES["latlng_pair"])]})
        after = _location_state(location)
        if not created and after == before:
            return location
        location.save()
        diff_before, diff_after = (None, after) if created else _diff(before, after)
        audit.record(
            action="location.created" if created else "location.updated",
            entity_type="location",
            entity_id=str(location.pk),
            actor=actor,
            request=request,
            before=diff_before,
            after=diff_after,
        )
    return location
