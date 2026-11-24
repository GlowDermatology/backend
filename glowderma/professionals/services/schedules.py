"""A specialist's schedule (FND-1): the weekly hours, the date overrides and the blocked periods."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.professionals.models import (
    BlockedPeriod,
    Professional,
    WorkingHours,
    WorkingHoursOverride,
)
from glowderma.professionals.services import common


def _week_state(professional: Professional) -> list[dict[str, Any]]:
    return [
        {
            "weekday": row.weekday,
            "location_id": row.location_id,
            "start": common.clock(row.start),
            "end": common.clock(row.end),
            "break_start": common.clock(row.break_start),
            "break_end": common.clock(row.break_end),
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
        professional = common.locked_professional(professional_id)
        before = _week_state(professional)
        rows = {row.weekday: row for row in WorkingHours.objects.filter(professional=professional)}
        for day in days:
            row = rows.pop(day["weekday"], None) or WorkingHours(
                professional=professional, weekday=day["weekday"]
            )
            values = {key: day.get(key) for key in ("start", "end", "break_start", "break_end")}
            values["location_id"] = common.schedule_location_id(
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
        "start": common.clock(row.start),
        "end": common.clock(row.end),
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
        professional = common.locked_professional(professional_id)
        rows = WorkingHoursOverride.objects.filter(professional=professional)
        row = rows.filter(pk=override_id).first() if override_id is not None else None
        if override_id is not None and row is None:
            raise NotFound
        if row is not None:
            rows = rows.exclude(pk=row.pk)
        if rows.filter(date=data["date"]).exists():
            raise ApiValidationError({"date": [field_error("unique", common.API_MESSAGES["override_taken"])]})
        before = _override_state(row) if row else None
        if row is None:
            row = WorkingHoursOverride(professional=professional, created_by=actor)
        row.date = data["date"]
        row.start = data.get("start")
        row.end = data.get("end")
        row.location_id = (
            common.schedule_location_id(professional, data.get("location_id", professional.location_id))
            if row.start is not None
            else None
        )
        row.note = data.get("note", "")
        common.save_unique(
            row.save, common.OVERRIDE_CONSTRAINT, "date", common.API_MESSAGES["override_taken"]
        )
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
        professional = common.locked_professional(professional_id)
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
        professional = common.locked_professional(professional_id)
        start, end = data["start"], data["end"]
        rows = BlockedPeriod.objects.filter(professional=professional)
        block = rows.filter(pk=block_id).first() if block_id is not None else None
        if block_id is not None and block is None:
            raise NotFound
        if block is not None:
            rows = rows.exclude(pk=block.pk)
        if rows.filter(start__lt=end, end__gt=start).exists():
            raise ApiValidationError(
                {"start": [field_error("overlap", common.API_MESSAGES["block_overlap"])]}
            )
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
        professional = common.locked_professional(professional_id)
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


__all__ = [
    "create_block",
    "create_override",
    "delete_block",
    "delete_override",
    "set_working_week",
]
