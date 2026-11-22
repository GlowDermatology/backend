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
from django.utils.translation import gettext, gettext_lazy, ngettext
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts.models import StaffUser
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import Conflict, field_error
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import media_name
from glowderma.core.uzsource import N_
from glowderma.core.variants import delete_with_variants, store_variants
from glowderma.professionals import bookings, selectors
from glowderma.professionals.models import (
    PROFESSIONAL_SLUG_CONSTRAINT,
    SERVICE_NAME_CONSTRAINT,
    BlockedPeriod,
    Location,
    Professional,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)

PHOTO_KIND = "professionals"
# Audit entity names (contract §3.2); the override is audited the same way (design 03 open point S-3 / T13).
SPECIALIST_LINK_ERROR = N_("Mutaxassis profili o‘z hisobiga butun umri davomida bog‘langan.")
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


def store_photo(professional: Professional, upload: Any) -> None:
    """Process (``core.images``), name and save ``upload`` as the professional's photo (the admin upload path
    and the demo seed)."""
    _store_photo(professional, upload)


def _store_photo(professional: Professional, upload: Any) -> None:
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ValidationError({"photo": IMAGE_MESSAGES[exc.code]}) from exc
    name = media_name(PHOTO_KIND, professional.pk, processed.content, processed.extension)
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


def _refuse_deactivation_with_upcoming(professional: Professional) -> None:
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
        _refuse_deactivation_with_upcoming(obj)
    if isinstance(obj, Professional) and obj.is_active and not (before and before["is_active"]):
        missing = selectors.publish_blockers(obj)
        if missing:
            raise ValidationError(
                {
                    "is_active": gettext(PUBLISH_BLOCKED)
                    % {"missing": ", ".join(gettext(PUBLISH_BLOCKER_MESSAGES[key]) for key in missing)}
                }
            )
    if isinstance(obj, Location) and before is not None and before["is_active"] and not obj.is_active:
        if _clinic_in_use(obj.pk):
            raise ValidationError({"is_active": gettext_lazy(API_MESSAGES["clinic_in_use"])})
    if isinstance(obj, Professional) and obj.location_id is not None:
        existing = (
            {before["location_id"], *obj.additional_locations.values_list("pk", flat=True)}
            if before
            else set()
        )
        target = {obj.location_id, *(existing - {before["location_id"]} if before else ())}
        _check_clinic_assignments(obj, target, existing, form=True)
    if isinstance(obj, Service) and obj.professional_id is not None:
        Professional.objects.select_for_update().get(pk=obj.professional_id)  # specialist → service order
    if isinstance(obj, (WorkingHours, WorkingHoursOverride)) and obj.professional_id is not None:
        professional = Professional.objects.select_for_update().get(pk=obj.professional_id)
        if obj.location_id is not None:
            try:
                _schedule_location_id(professional, obj.location_id)
            except ApiValidationError as exc:
                raise ValidationError(
                    {"location": gettext_lazy(API_MESSAGES["clinic_not_assigned"])}
                ) from exc
    pending_photo = None
    if isinstance(obj, Professional) and obj.photo and not getattr(obj.photo, "_committed", True):
        # The content-addressed name needs the row id: validate the bytes now, store them after the insert.
        pending_photo = obj.photo.file
        obj.photo = None if before is None else before.get("photo") or None
    _validate(obj)
    obj.save()
    if isinstance(obj, Professional) and obj.location_id is not None:
        obj.additional_locations.remove(obj.location_id)
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


# --- named helpers ----------------------------------------------------------------------------------------


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
            location_id=_schedule_location_id(professional, professional.location_id),
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
    "clinic_not_assigned": N_("Faol filialni ushbu mutaxassisga biriktiring."),
    "clinic_not_found": N_("Faol filial topilmadi."),
    "clinic_primary_required": N_("Asosiy filial biriktirilgan filiallar orasida bo‘lishi kerak."),
    "clinic_scheduled": N_("Filialni ajratishdan oldin ish jadvalidagi filialni o‘zgartiring."),
    "clinic_in_use": N_(
        "Filialni nofaol qilishdan oldin faol mutaxassislarni ajrating va kelgusi qabullarni ko‘chiring "
        "yoki bekor qiling."
    ),
    "staff_taken": N_("Bu xodim boshqa mutaxassisga bog‘langan."),
    "has_upcoming": N_(
        "Mutaxassisda {n} ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Qabullar o‘zgarmaydi: avval "
        "ularni boshqa vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling, yoki baribir nofaol qiling."
    ),
    "override_taken": N_("Bu sana uchun alohida jadval allaqachon bor."),
    "block_overlap": N_("Bu vaqt boshqa band vaqt bilan kesishadi."),
    "latlng_pair": N_("Kenglik va uzunlikni birga kiriting."),
    "slug_taken": N_("Bu manzil (slug) boshqa mutaxassisda bor."),
    "service_name_taken": N_("Bu mutaxassisda shu nomdagi xizmat allaqachon bor."),
    "service_limit": N_("Bitta mutaxassisda ko‘pi bilan %(n)s ta xizmat bo‘lishi mumkin."),
    "service_booked": N_(
        "Bu xizmatga qabullar yozilgan, uni o‘chirib bo‘lmaydi. Uning o‘rniga nofaol qiling."
    ),
}
MAX_SERVICES_PER_SPECIALIST = 50


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


def _next_position(model: type[Professional] | type[Service] | type[Location]) -> int:
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


def _save_specialist(professional: Professional) -> None:
    """Save with both unique races (the staff link, the B-W1 slug) reported as field errors, not a 500."""

    def save() -> None:
        _save_unique(professional.save, PROFESSIONAL_SLUG_CONSTRAINT, "slug", API_MESSAGES["slug_taken"])

    _save_unique(save, STAFF_USER_CONSTRAINT, "staff_user_id", API_MESSAGES["staff_taken"])


def _checked_clinics(professional: Professional, data: dict[str, Any], current_ids: set[int]) -> set[int]:
    ids = set(data.get("location_ids", current_ids))
    if professional.location_id not in ids:
        raise ApiValidationError(
            {"location_ids": [field_error("invalid", API_MESSAGES["clinic_primary_required"])]}
        )
    _check_clinic_assignments(professional, ids, current_ids)
    return ids


def _check_clinic_assignments(
    professional: Professional, ids: set[int], current_ids: set[int], *, form: bool = False
) -> None:
    # Match the booking lock order: the specialist is already locked, then clinics in PK order.
    clinics = dict(
        Location.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk", "is_active")
    )
    if ids != set(clinics) or any(
        not is_active and (professional.is_active or pk not in current_ids)
        for pk, is_active in clinics.items()
    ):
        if form:
            raise ValidationError({"location": gettext_lazy(API_MESSAGES["clinic_not_found"])})
        raise ApiValidationError(
            {"location_ids": [field_error("does_not_exist", API_MESSAGES["clinic_not_found"])]}
        )
    removed = current_ids - ids
    if removed and (
        professional.working_hours.filter(location_id__in=removed).exists()
        or professional.hours_overrides.filter(
            location_id__in=removed, date__gte=timeutils.local_date(timeutils.now())
        ).exists()
    ):
        if form:
            raise ValidationError({"location": gettext_lazy(API_MESSAGES["clinic_scheduled"])})
        raise Conflict(API_MESSAGES["clinic_scheduled"])


def _schedule_location_id(professional: Professional, location_id: int | None) -> int:
    if (
        location_id is None
        or not selectors.has_location(professional, location_id)
        or not Location.objects.filter(pk=location_id, is_active=True).exists()
    ):
        raise ApiValidationError(
            {"location_id": [field_error("does_not_exist", API_MESSAGES["clinic_not_assigned"])]}
        )
    return location_id


def _specialist_state(professional: Professional, location_ids: set[int]) -> dict[str, Any]:
    return {
        "name": professional.name,
        "role": professional.role,
        **_i18n_state(professional, "qualifications"),
        **_i18n_state(professional, "bio"),
        "experience_years": professional.experience_years,
        "slot_step_min": professional.slot_step_min,
        "location_id": professional.location_id,
        "location_ids": sorted(location_ids),
        "staff_user_id": professional.staff_user_id,
        "position": professional.position,
        "is_active": professional.is_active,
        "slug": professional.slug,
    }


SPECIALIST_SCALARS = ("name", "role", "experience_years", "slot_step_min", "position", "is_active")
# What a specialist may edit on their own profile (``specialist/profile/``).
OWN_PROFILE_KEYS = frozenset({"name", "bio"})
PUBLISH_BLOCKER_MESSAGES = {
    "account": N_("hisob Mutaxassis rolida emas"),
    "name": N_("ommaviy ism"),
    "discipline": N_("yo‘nalish"),
    "bio": N_("bio (o‘zbekcha)"),
    "photo": N_("surat"),
    "clinic": N_("faol asosiy filial"),
    "service": N_("faol xizmat"),
    "availability": N_("ish vaqti"),
}
PUBLISH_BLOCKED = N_("Profilni e’lon qilib bo‘lmaydi. Yetishmayapti: %(missing)s.")


def _apply_specialist(professional: Professional, data: dict[str, Any]) -> None:
    for key in SPECIALIST_SCALARS:
        if key in data:
            setattr(professional, key, data[key])
    if "location_id" in data:
        professional.location_id = data["location_id"]
    if data.get("slug"):
        professional.slug = data["slug"]  # B-W1: staff edit the website URL segment
    _set_i18n(professional, "qualifications", data.get("qualifications"))
    _set_i18n(professional, "bio", data.get("bio"))


def _refuse_unpublishable(professional: Professional) -> None:
    missing = selectors.publish_blockers(professional)
    if missing:
        raise Conflict(
            PUBLISH_BLOCKED % {"missing": ", ".join(str(PUBLISH_BLOCKER_MESSAGES[key]) for key in missing)}
        )


def _create_draft_profile(user: StaffUser, *, actor: Any, request: Any) -> Professional:
    """The private draft profile owned by ``user`` for its lifetime: named after the account, no clinic,
    service or availability yet, not published (staff publish it once it is complete)."""
    professional = Professional(
        staff_user=user, name=user.full_name, position=_next_position(Professional), is_active=False
    )
    _save_specialist(professional)
    audit.record(
        action="professional.created",
        entity_type="professional",
        entity_id=str(professional.pk),
        actor=actor,
        request=request,
        after=_specialist_state(professional, set()),
    )
    return professional


def update_specialist(
    professional_id: int, data: dict[str, Any], *, force: bool = False, actor: Any, request: Any = None
) -> Professional:
    """``PATCH specialists/{id}/`` (staff) and ``PATCH specialist/profile/`` (the owner, ``name`` / ``bio``).

    ``is_active`` is publication: publishing needs the owner account in the Specialist role and a complete
    profile (:func:`selectors.publish_blockers`, else 409 naming what is missing). Unpublishing a
    profile with ``requested`` / ``confirmed`` appointments ahead is refused (409 naming the count) unless
    ``force``; the appointments are kept either way. Audit ``professional.deactivated`` /
    ``professional.activated`` when ``is_active`` flips (with the other changed fields), else
    ``professional.updated``; nothing changed → no write, no audit."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        current_ids = set(selectors.assigned_location_ids(professional))
        before = _specialist_state(professional, current_ids)
        _apply_specialist(professional, data)
        location_ids = current_ids
        if "location_id" in data or "location_ids" in data:
            location_ids = _checked_clinics(professional, data, current_ids)
        after = _specialist_state(professional, location_ids)
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
        _save_specialist(professional)
        if before["location_id"] != professional.location_id or location_ids != current_ids:
            professional.additional_locations.set(location_ids - {professional.location_id})
        if action == "professional.activated":
            # Checked after the write (the clinics are part of it); the 409 rolls the whole change back.
            _refuse_unpublishable(professional)
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


def update_own_profile(
    professional_id: int, data: dict[str, Any], *, actor: Any, request: Any = None
) -> Professional:
    """A specialist edits their public name and bio; any other key is a programming error."""
    unknown = set(data) - OWN_PROFILE_KEYS
    if unknown:
        raise ValueError(f"Not editable by the specialist: {sorted(unknown)}")
    return update_specialist(professional_id, data, actor=actor, request=request)


@transaction.atomic
def invite_specialist(
    data: dict[str, Any], *, actor: StaffUser, request: Any = None
) -> tuple[StaffUser, str | None, Professional]:
    """Staff → Add employee → Specialist: create the Specialist account and its draft profile together.

    An email already in use is the usual ``unique`` field error, so a duplicate invite never creates a second
    account or profile; resending the invitation goes through the account's own password routes."""
    user, password = account_services.create_staff(
        **data, role=enums.StaffRole.SPECIALIST, actor=actor, request=request
    )
    professional = _create_draft_profile(user, actor=actor, request=request)
    return user, password, professional


@transaction.atomic
def set_specialist_account_role(
    staff_user_id: int, role: str, *, actor: StaffUser, request: Any = None
) -> tuple[StaffUser, Professional | None, int]:
    """Staff → Edit: the confirmed ``staff`` ↔ ``specialist`` role change of one account.

    → ``specialist``: an eligible Staff or Manager account becomes a Specialist; its draft profile is created
    if it never had one, else the profile it already owns is reused as it is (unpublished since it left the
    role: staff review it before republishing). → ``staff``: specialist access ends and the profile is
    unpublished (no new bookings); the profile and its appointments stay attached to the account and the
    count of outstanding visits is returned for staff to handle. The accounts service keeps the protections
    (own account, inactive, superuser, operations-console access, last active manager) and revokes the
    account's sessions on a change. Asking for the role the account already has changes nothing, so a retry
    never creates a second profile. Returns ``(account, profile, upcoming appointments)``."""
    profile = Professional.objects.select_for_update().filter(staff_user_id=staff_user_id).first()
    to_specialist = role == enums.StaffRole.SPECIALIST
    user = account_services.change_specialist_role(
        staff_user_id,
        enums.StaffRole.SPECIALIST if to_specialist else enums.StaffRole.ADMIN,
        actor=actor,
        request=request,
    )
    if to_specialist:
        if profile is None:
            profile = _create_draft_profile(user, actor=actor, request=request)
        return user, profile, 0
    if profile is None:
        return user, None, 0
    upcoming = bookings.upcoming_counts([profile.pk], timeutils.now())[profile.pk]
    if profile.is_active:
        profile.is_active = False
        profile.save(update_fields=["is_active", "updated_at"])
        audit.record(
            action="professional.deactivated",
            entity_type="professional",
            entity_id=str(profile.pk),
            actor=actor,
            request=request,
            before={"is_active": True},
            after={"is_active": False, "upcoming_appointments": upcoming, "reason": "role_changed"},
        )
    return user, profile, upcoming


def set_specialist_photo(
    professional_id: int, upload: Any, *, actor: Any, request: Any = None
) -> Professional:
    """``PUT specialists/{id}/photo/``: re-encoded by ``core.images`` (the product-image limits), stored under
    a content-addressed name; the previous file is removed after commit. Audit ``professional.photo_updated``
    (the same bytes again change nothing).

    The new file is written under the row lock (local storage today). If this block fails after the write
    (the audit insert, say), the new file is removed again so a rollback leaves no orphan; a rollback of an
    enclosing transaction (the Idempotency-Key wrapper) after this block returned cannot be seen here. A
    remote storage backend should move the upload before the lock (catalog banners share this shape)."""
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ApiValidationError({"file": [field_error(exc.code)]}) from exc
    written, storage = "", None
    try:
        with transaction.atomic():
            professional = _locked_professional(professional_id)
            previous = professional.photo.name or ""
            name = media_name(PHOTO_KIND, professional.pk, processed.content, processed.extension)
            if previous == name:
                # nothing replaced; a variant that went missing (or predates the variants) is written again
                store_variants(professional.photo.storage, previous, processed.content)
                return professional
            professional.photo.save(name.rsplit("/", 1)[-1], ContentFile(processed.content), save=False)
            written, storage = professional.photo.name or "", professional.photo.storage
            store_variants(storage, written, processed.content)
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
    except BaseException:
        if written and storage is not None:
            delete_with_variants(storage, written)
        raise
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
    transaction.on_commit(lambda: delete_with_variants(storage, name))


def _week_state(professional: Professional) -> list[dict[str, Any]]:
    return [
        {
            "weekday": row.weekday,
            "location_id": row.location_id,
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
            values["location_id"] = _schedule_location_id(
                professional, day.get("location_id", professional.location_id)
            )
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
        "location_id": row.location_id,
        "start": _clock(row.start),
        "end": _clock(row.end),
        "note": row.note,
    }


def create_override(
    professional_id: int,
    data: dict[str, Any],
    *,
    actor: Any,
    request: Any = None,
    override_id: int | None = None,
) -> WorkingHoursOverride:
    """``POST specialists/{id}/overrides/``: one date's own hours (both ``None`` = day off); one per date
    (400 ``date`` ``unique``). Audit ``schedule.override_created``."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        rows = WorkingHoursOverride.objects.filter(professional=professional)
        row = rows.filter(pk=override_id).first() if override_id is not None else None
        if override_id is not None and row is None:
            raise NotFound
        if row is not None:
            rows = rows.exclude(pk=row.pk)
        if rows.filter(date=data["date"]).exists():
            raise ApiValidationError({"date": [field_error("unique", API_MESSAGES["override_taken"])]})
        before = _override_state(row) if row else None
        if row is None:
            row = WorkingHoursOverride(professional=professional, created_by=actor)
        row.date = data["date"]
        row.start = data.get("start")
        row.end = data.get("end")
        row.location_id = (
            _schedule_location_id(professional, data.get("location_id", professional.location_id))
            if row.start is not None
            else None
        )
        row.note = data.get("note", "")
        _save_unique(row.save, OVERRIDE_CONSTRAINT, "date", API_MESSAGES["override_taken"])
        audit.record(
            action="schedule.override_updated" if before else "schedule.override_created",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before=before,
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
    professional_id: int,
    data: dict[str, Any],
    *,
    actor: Any,
    request: Any = None,
    block_id: int | None = None,
) -> BlockedPeriod:
    """``POST specialists/{id}/blocks/``: an absolute busy / off range that overlaps none of the
    specialist's other blocks (400 ``start`` ``overlap``). Appointments inside it are kept (they show up in
    ``conflicts/``). Audit ``schedule.block_created``."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        start, end = data["start"], data["end"]
        rows = BlockedPeriod.objects.filter(professional=professional)
        block = rows.filter(pk=block_id).first() if block_id is not None else None
        if block_id is not None and block is None:
            raise NotFound
        if block is not None:
            rows = rows.exclude(pk=block.pk)
        if rows.filter(start__lt=end, end__gt=start).exists():
            raise ApiValidationError({"start": [field_error("overlap", API_MESSAGES["block_overlap"])]})
        before = _block_state(block) if block else None
        if block is None:
            block = BlockedPeriod(professional=professional, created_by=actor)
        block.start, block.end = start, end
        block.kind = data["kind"]
        block.label = data.get("label") or ""
        block.save()
        audit.record(
            action="schedule.block_updated" if before else "schedule.block_created",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before=before,
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
        "professional_id": service.professional_id,
        **_i18n_state(service, "name"),
        **_i18n_state(service, "short_name"),
        **_i18n_state(service, "description"),
        "category": service.category,
        "price": service.price,
        "duration_min": service.duration_min,
        "position": service.position,
        "is_active": service.is_active,
    }


SERVICE_SCALARS = ("category", "price", "duration_min", "position", "is_active")


def _apply_service(service: Service, data: dict[str, Any]) -> None:
    for key in SERVICE_SCALARS:
        if key in data:
            setattr(service, key, data[key])
    for prefix in ("name", "short_name", "description"):
        _set_i18n(service, prefix, data.get(prefix))


def _save_service(service: Service) -> None:
    """Save with the per-specialist name race (``service_professional_name_unique``) as 400 ``unique``."""
    _save_unique(service.save, SERVICE_NAME_CONSTRAINT, "name", API_MESSAGES["service_name_taken"])


def _owned_service(professional_id: int, service_id: int) -> Service:
    """Lock the specialist, then its service (the booking order: specialist → service); another
    specialist's service is 404."""
    professional = _locked_professional(professional_id)
    service = (
        Service.objects.select_for_update().filter(pk=service_id, professional_id=professional.pk).first()
    )
    if service is None:
        raise NotFound
    return service


def default_service_category(professional: Professional) -> str:
    """A new service's discipline when none is given: the specialist's own (dermatology for a draft)."""
    if professional.role == enums.SpecialistRole.COSMETOLOGIST:
        return str(enums.ServiceCategory.COSMETOLOGY)
    return str(enums.ServiceCategory.DERMATOLOGY)


def create_specialist_service(
    professional_id: int, data: dict[str, Any], *, actor: Any, request: Any = None
) -> Service:
    """``POST specialists/{id}/services/`` (staff) and ``specialist/services/`` (the owner): a new service of
    that specialist, appended after its others unless a ``position`` is given; the category defaults from
    the specialist's role. A repeated name of the same specialist is 400 ``unique``; more than
    :data:`MAX_SERVICES_PER_SPECIALIST` is 409. Audit ``service.created``."""
    with transaction.atomic():
        professional = _locked_professional(professional_id)
        owned = Service.objects.filter(professional=professional)
        if owned.count() >= MAX_SERVICES_PER_SPECIALIST:
            raise Conflict(API_MESSAGES["service_limit"] % {"n": MAX_SERVICES_PER_SPECIALIST})
        last = owned.order_by("-position").values_list("position", flat=True).first()
        service = Service(
            professional=professional,
            position=0 if last is None else int(last) + 1,
            category=default_service_category(professional),
        )
        _apply_service(service, data)
        _save_service(service)
        audit.record(
            action="service.created",
            entity_type="service",
            entity_id=str(service.pk),
            actor=actor,
            request=request,
            after=_service_state(service),
        )
    return service


def update_specialist_service(
    professional_id: int, service_id: int, data: dict[str, Any], *, actor: Any, request: Any = None
) -> Service:
    """``PATCH …/services/{id}/``: ``is_active: false`` takes a service out of the customer catalogue and the
    booking dialogs at once; booked appointments keep their price and duration snapshots. Audit
    ``service.deactivated`` / ``service.activated`` when ``is_active`` flips, else ``service.updated``;
    nothing changed → no write, no audit."""
    with transaction.atomic():
        service = _owned_service(professional_id, service_id)
        before = _service_state(service)
        _apply_service(service, data)
        after = _service_state(service)
        if after == before:
            return service
        action = "service.updated"
        if before["is_active"] != after["is_active"]:
            action = "service.activated" if after["is_active"] else "service.deactivated"
        _save_service(service)
        diff_before, diff_after = _diff(before, after)
        audit.record(
            action=action,
            entity_type="service",
            entity_id=str(service.pk),
            actor=actor,
            request=request,
            before={"professional_id": service.professional_id, **diff_before},
            after={"professional_id": service.professional_id, **diff_after},
        )
    return service


def delete_specialist_service(
    professional_id: int, service_id: int, *, actor: Any, request: Any = None
) -> None:
    """``DELETE …/services/{id}/``: only a service that was never booked (``Appointment.service`` is
    ``PROTECT``); one with appointments, past or cancelled included, is 409 (deactivate it instead).
    Audit ``service.deleted``."""
    with transaction.atomic():
        service = _owned_service(professional_id, service_id)
        state = _service_state(service)
        pk = service.pk
        try:
            with transaction.atomic():
                service.delete()
        except ProtectedError as exc:
            raise Conflict(API_MESSAGES["service_booked"]) from exc
        audit.record(
            action="service.deleted",
            entity_type="service",
            entity_id=str(pk),
            actor=actor,
            request=request,
            before=state,
        )


def _location_state(location: Location) -> dict[str, Any]:
    return {
        **_i18n_state(location, "name"),
        **_i18n_state(location, "address"),
        **_i18n_state(location, "directions"),
        "is_active": location.is_active,
        "phone": location.phone or None,
        "latitude": None if location.latitude is None else str(location.latitude),
        "longitude": None if location.longitude is None else str(location.longitude),
    }


def _clinic_in_use(location_id: int) -> bool:
    return Professional.objects.filter(is_active=True).filter(
        models.Q(location_id=location_id) | models.Q(additional_locations=location_id)
    ).exists() or bookings.location_has_upcoming(location_id, timeutils.now())


@transaction.atomic
def create_location(data: dict[str, Any], *, actor: Any, request: Any = None) -> Location:
    _advisory_lock(LOCATION_LOCK)
    return _write_location(Location(position=_next_position(Location)), data, actor=actor, request=request)


@transaction.atomic
def update_location(location_id: int, data: dict[str, Any], *, actor: Any, request: Any = None) -> Location:
    location = Location.objects.select_for_update().filter(pk=location_id).first()
    if location is None:
        raise NotFound
    return _write_location(location, data, actor=actor, request=request)


def _write_location(location: Location, data: dict[str, Any], *, actor: Any, request: Any) -> Location:
    before = _location_state(location) if location.pk is not None else None
    for field in ("name", "address", "directions"):
        _set_i18n(location, field, data.get(field))
    if "phone" in data:
        location.phone = data["phone"] or ""
    for key in ("latitude", "longitude", "is_active"):
        if key in data:
            setattr(location, key, data[key])
    if (location.latitude is None) != (location.longitude is None):
        missing = "latitude" if location.latitude is None else "longitude"
        raise ApiValidationError({missing: [field_error("latlng_pair", API_MESSAGES["latlng_pair"])]})
    after = _location_state(location)
    if after == before:
        return location
    if before and before["is_active"] and not location.is_active and _clinic_in_use(location.pk):
        raise Conflict(API_MESSAGES["clinic_in_use"])
    location.save()
    diff_before, diff_after = (None, after) if before is None else _diff(before, after)
    audit.record(
        action="location.created" if before is None else "location.updated",
        entity_type="location",
        entity_id=str(location.pk),
        actor=actor,
        request=request,
        before=diff_before,
        after=diff_after,
    )
    return location
