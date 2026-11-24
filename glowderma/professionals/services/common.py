"""What the admin API writes of `professionals` share: the value limits, the refusal texts, the locks and the
save helpers.

Writes of ``api/admin/`` (contract "Mutaxassislar va xizmatlar") share these rules. The serializers validate
shapes and value rules and resolve references; the services own the state rules (uniqueness, overlaps, the
deactivation guard), run under the specialist's row lock (``SELECT … FOR UPDATE``: concurrent edits of one
specialist serialise, and a booking's ``FOR KEY SHARE`` of the specialist — ``appointments.services`` —
waits for a deactivation or makes it wait) and write one audit row per operation, only when something
changed. Existing appointments are never touched: they keep their time, price and duration snapshots
whatever the schedule, the offered services or the prices become."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from django.db import IntegrityError, connection, models, transaction
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.core.api.exceptions import field_error
from glowderma.core.uzsource import N_
from glowderma.professionals import selectors
from glowderma.professionals.models import (
    Location,
    Professional,
    Service,
)

PHOTO_KIND = "professionals"
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


def advisory_lock(key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


def violates(exc: IntegrityError, constraint: str) -> bool:
    name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
    if name is not None:
        return bool(name == constraint)
    return constraint in str(exc)  # pragma: no cover - a driver without diagnostics


def save_unique(save: Callable[[], None], constraint: str, field: str, message: str) -> None:
    """``save`` in a savepoint; losing the race on ``constraint`` becomes 400 ``unique`` on ``field``."""
    try:
        with transaction.atomic():
            save()
    except IntegrityError as exc:
        if not violates(exc, constraint):
            raise
        raise ApiValidationError({field: [field_error("unique", message)]}) from exc


def diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    changed = [key for key in after if before.get(key) != after[key]]
    return {key: before.get(key) for key in changed}, {key: after[key] for key in changed}


def next_position(model: type[Professional] | type[Service] | type[Location]) -> int:
    last = model.objects.order_by("-position").values_list("position", flat=True).first()
    return 0 if last is None else int(last) + 1


def clock(value: dt.time | None) -> str | None:
    return None if value is None else value.strftime("%H:%M")


def locked_professional(pk: int) -> Professional:
    professional = Professional.objects.select_for_update().filter(pk=pk).first()
    if professional is None:
        raise NotFound
    return professional


def i18n_state(obj: models.Model, prefix: str) -> dict[str, str]:
    return {f"{prefix}.{language}": getattr(obj, f"{prefix}_{language}") for language in ("uz", "ru", "en")}


def set_i18n(obj: models.Model, prefix: str, texts: dict[str, str] | None) -> None:
    for language, value in (texts or {}).items():
        setattr(obj, f"{prefix}_{language}", value)


def schedule_location_id(professional: Professional, location_id: int | None) -> int:
    if (
        location_id is None
        or not selectors.has_location(professional, location_id)
        or not Location.objects.filter(pk=location_id, is_active=True).exists()
    ):
        raise ApiValidationError(
            {"location_id": [field_error("does_not_exist", API_MESSAGES["clinic_not_assigned"])]}
        )
    return location_id


__all__ = [
    "PHOTO_KIND",
    "API_MESSAGES",
    "BLOCK_MAX_DAYS",
    "DURATION_STEP_MIN",
    "LOCATION_LOCK",
    "MAX_PRICE",
    "MAX_SERVICES_PER_SPECIALIST",
    "OVERRIDE_CONSTRAINT",
    "OVERRIDE_HORIZON_DAYS",
    "STAFF_USER_CONSTRAINT",
    "advisory_lock",
    "clock",
    "diff",
    "i18n_state",
    "locked_professional",
    "next_position",
    "save_unique",
    "schedule_location_id",
    "set_i18n",
    "violates",
]
