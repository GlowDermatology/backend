"""Django admin for the professionals models (design 01 §11, 03 §8).

The seven models ``Location``, ``Professional``, ``Service``, ``ProfessionalService``, ``WorkingHours``,
``WorkingHoursOverride`` (decision T13) and ``BlockedPeriod`` are the only editable ones in the whole Django
admin; every write goes through :mod:`glowderma.professionals.services` (``save_model`` / ``delete_model`` /
the weekly-hours inline on a professional), which validates and audits ``<model>.created|updated|deleted``.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.db import models
from django.http import HttpRequest
from unfold.widgets import UnfoldAdminTimeWidget

from glowderma.core.admin import OpsTabularInline, ServiceRefused, is_active_staff
from glowderma.core.admin import ServiceBackedAdmin as OpsServiceBackedAdmin
from glowderma.professionals import services
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)

# Working hours are whole minutes: shown and typed as ``09:00``.
TIME_FORMAT = "%H:%M"

__all__ = ["ServiceBackedAdmin", "ServiceRefused", "WorkingHoursInline"]


class ServiceBackedAdmin(OpsServiceBackedAdmin):
    """The console's service-backed admin (:class:`glowderma.core.admin.ServiceBackedAdmin`) over
    :mod:`glowderma.professionals.services`: every row (inline weekly hours included) is written by
    ``save_row`` and deleted by ``delete_row``, which validate and audit
    ``<model>.created|updated|deleted``."""

    def service_save(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        services.save_row(obj, actor=request.user, request=request)

    def service_delete(self, request: HttpRequest, obj: models.Model) -> None:
        services.delete_row(obj, actor=request.user, request=request)


class WorkingHoursInline(OpsTabularInline):
    """The weekly hours on a professional's page: weekday as a select of Uzbek day names (the column's
    choices), times as ``09:00`` (no seconds), no per-row caption (the rows are one table)."""

    model = WorkingHours
    extra = 0
    fields = ("weekday", "start", "end", "break_start", "break_end")
    show_title = False

    def formfield_for_dbfield(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        if getattr(db_field, "null", False) and not db_field.blank:
            kwargs.setdefault("required", False)
        if isinstance(db_field, models.TimeField):
            kwargs["widget"] = UnfoldAdminTimeWidget(format=TIME_FORMAT)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def has_view_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)

    def has_add_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)


@admin.register(Location)
class LocationAdmin(ServiceBackedAdmin):
    list_display = ("id", "name_uz", "address_uz", "phone", "is_active", "position")
    list_filter = ("is_active",)
    search_fields = ("name_uz", "name_ru", "name_en", "address_uz")
    ordering = ("position", "id")


@admin.register(Professional)
class ProfessionalAdmin(ServiceBackedAdmin):
    list_display = ("id", "name", "role", "location", "slot_step_min", "position", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("name",)
    ordering = ("position", "name", "id")
    autocomplete_fields = ("location",)
    inlines = (WorkingHoursInline,)


@admin.register(Service)
class ServiceAdmin(ServiceBackedAdmin):
    list_display = (
        "id",
        "name_uz",
        "short_name_uz",
        "default_duration_min",
        "default_price",
        "position",
        "is_active",
    )
    list_filter = ("is_active",)
    search_fields = ("name_uz", "name_ru", "name_en", "short_name_uz")
    ordering = ("position", "id")


@admin.register(ProfessionalService)
class ProfessionalServiceAdmin(ServiceBackedAdmin):
    list_display = ("id", "professional", "service", "price", "duration_min", "is_active")
    list_filter = ("is_active",)
    search_fields = ("professional__name", "service__name_uz")
    ordering = ("professional_id", "service_id")
    autocomplete_fields = ("professional", "service")


@admin.register(WorkingHours)
class WorkingHoursAdmin(ServiceBackedAdmin):
    list_display = ("id", "professional", "weekday", "start", "end", "break_start", "break_end")
    list_filter = ("weekday",)
    search_fields = ("professional__name",)
    ordering = ("professional_id", "weekday")
    autocomplete_fields = ("professional",)


@admin.register(BlockedPeriod)
class BlockedPeriodAdmin(ServiceBackedAdmin):
    list_display = ("id", "professional", "start", "end", "kind", "label", "created_by")
    list_filter = ("kind",)
    search_fields = ("professional__name", "label")
    ordering = ("-start", "id")
    autocomplete_fields = ("professional",)

    def save_model(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        if not change:
            obj.created_by = request.user  # type: ignore[attr-defined]
        super().save_model(request, obj, form, change)


@admin.register(WorkingHoursOverride)
class WorkingHoursOverrideAdmin(ServiceBackedAdmin):
    """A date's own hours (both empty = day off); one row per professional and date (T13)."""

    list_display = ("id", "professional", "date", "start", "end", "created_by")
    search_fields = ("professional__name",)
    ordering = ("-date", "id")
    autocomplete_fields = ("professional",)

    def save_model(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        if not change:
            obj.created_by = request.user  # type: ignore[attr-defined]
        super().save_model(request, obj, form, change)
