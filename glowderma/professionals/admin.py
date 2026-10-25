"""Django admin for the professionals models (design 01 §11, 03 §8).

The six models ``Location``, ``Professional``, ``Service``, ``ProfessionalService``, ``WorkingHours`` and
``BlockedPeriod`` are the only editable ones in the whole Django admin; every write goes through
:mod:`glowderma.professionals.services` (``save_model`` / ``delete_model`` / the weekly-hours inline on a
professional), which validates and audits ``<model>.created|updated|deleted``. ``WorkingHoursOverride`` stays
read-only in M1 (open point S-3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import models
from django.http import HttpRequest

from glowderma.core.admin import register_read_only
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

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[models.Model]
    _TabularInline = admin.TabularInline[models.Model, models.Model]
else:
    _ModelAdmin = admin.ModelAdmin
    _TabularInline = admin.TabularInline


def _is_active_staff(request: HttpRequest) -> bool:
    user = request.user
    return bool(user.is_active and user.is_staff)


def _report(request: HttpRequest, exc: ValidationError) -> None:
    for message in exc.messages:
        messages.error(request, message)


class ServiceBackedAdmin(_ModelAdmin):
    """Editable ``ModelAdmin`` whose writes are the services' writes (design 03 §8).

    Bulk actions are off (the delete action would bypass ``delete_model``); the changelist never runs the
    full count, like ``ReadOnlyAdmin``.
    """

    actions = None
    list_per_page = 50
    show_full_result_count = False

    # Bookkeeping columns are shown, never typed in: the services stamp them.
    BOOKKEEPING_FIELDS = ("created_at", "updated_at", "created_by")

    def get_readonly_fields(self, request: HttpRequest, obj: models.Model | None = None) -> list[str]:
        names = {field.name for field in self.model._meta.concrete_fields}
        readonly = [name for name in self.BOOKKEEPING_FIELDS if name in names]
        readonly.extend(name for name in self.readonly_fields if name not in readonly)
        return readonly

    def formfield_for_dbfield(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        # ``null=True`` columns (experience_years, a break, coordinates) are optional in the form: ``None``
        # is their "not set" value (the services validate the same way).
        if getattr(db_field, "null", False) and not db_field.blank:
            kwargs.setdefault("required", False)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return _is_active_staff(request)

    def has_view_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return _is_active_staff(request)

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)

    def save_model(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        try:
            services.save_row(obj, actor=request.user, request=request)
        except ValidationError as exc:  # the ModelForm validated already; this is the last line of defence
            _report(request, exc)

    def delete_model(self, request: HttpRequest, obj: models.Model) -> None:
        try:
            services.delete_row(obj, actor=request.user, request=request)
        except ValidationError as exc:
            _report(request, exc)

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: bool) -> None:
        # Inline rows (weekly hours of a professional) pass through the same services, one by one.
        for obj in formset.save(commit=False):
            try:
                services.save_row(obj, actor=request.user, request=request)
            except ValidationError as exc:
                _report(request, exc)
        for obj in formset.deleted_objects:
            try:
                services.delete_row(obj, actor=request.user, request=request)
            except ValidationError as exc:
                _report(request, exc)
        formset.save_m2m()


class WorkingHoursInline(_TabularInline):
    model = WorkingHours
    extra = 0
    fields = ("weekday", "start", "end", "break_start", "break_end")

    def formfield_for_dbfield(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        if getattr(db_field, "null", False) and not db_field.blank:
            kwargs.setdefault("required", False)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def has_view_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)

    def has_add_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)


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
    raw_id_fields = ("location",)
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
    raw_id_fields = ("professional", "service")


@admin.register(WorkingHours)
class WorkingHoursAdmin(ServiceBackedAdmin):
    list_display = ("id", "professional", "weekday", "start", "end", "break_start", "break_end")
    list_filter = ("weekday",)
    search_fields = ("professional__name",)
    ordering = ("professional_id", "weekday")
    raw_id_fields = ("professional",)


@admin.register(BlockedPeriod)
class BlockedPeriodAdmin(ServiceBackedAdmin):
    list_display = ("id", "professional", "start", "end", "kind", "label", "created_by")
    list_filter = ("kind",)
    search_fields = ("professional__name", "label")
    ordering = ("-start", "id")
    raw_id_fields = ("professional",)

    def save_model(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        if not change:
            obj.created_by = request.user  # type: ignore[attr-defined]
        super().save_model(request, obj, form, change)


register_read_only(
    WorkingHoursOverride,
    list_display=("id", "professional", "date", "start", "end", "created_by"),
    search_fields=("professional__name",),
    ordering=("-date", "id"),
    raw_id_fields=("professional", "created_by"),
)
