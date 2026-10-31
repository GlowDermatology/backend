"""Django admin for the professionals models (design 01 §11, 03 §8).

The seven models ``Location``, ``Professional``, ``Service``, ``ProfessionalService``, ``WorkingHours``,
``WorkingHoursOverride`` (decision T13) and ``BlockedPeriod`` are the only editable ones in the whole Django
admin; every write goes through :mod:`glowderma.professionals.services` (``save_model`` / ``delete_model`` /
the weekly-hours inline on a professional), which validates and audits ``<model>.created|updated|deleted``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import models
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse

from glowderma.core.admin import OpsModelAdmin, OpsTabularInline, is_active_staff
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


class ServiceRefused(Exception):
    """A service ``ValidationError`` raised out of ``save_model`` / ``save_formset`` / ``delete_model``.

    Raising (instead of swallowing) lets the atomic ``changeform_view`` / ``delete_view`` roll back the whole
    request — nothing is saved, no ``LogEntry`` and no success message are written — and the view below turns
    it into a re-rendered form (or an error message on the change page for a delete).
    """

    def __init__(self, error: ValidationError) -> None:
        super().__init__(error)
        self.error = error


# The refusal of the first (rolled back) pass, read by the form of the second pass of the same request.
_REFUSAL: ContextVar[ValidationError | None] = ContextVar("professionals_admin_refusal", default=None)


def _attach(form: Any, error: ValidationError) -> None:
    """Put the service's errors on the form: per field when the form has that field, else non-field."""
    if hasattr(error, "error_dict"):
        for name, errors in error.error_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, error)


class ServiceBackedAdmin(OpsModelAdmin):
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
        return is_active_staff(request)

    def has_view_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return is_active_staff(request)

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)

    def get_form(
        self, request: HttpRequest, obj: models.Model | None = None, change: bool = False, **kwargs: Any
    ) -> Any:
        form_class = super().get_form(request, obj, change, **kwargs)
        current = _REFUSAL.get()
        if current is None:
            return form_class
        refusal: ValidationError = current

        class RefusedForm(form_class):  # type: ignore[misc,valid-type]
            def clean(self) -> Any:
                cleaned = super().clean()
                _attach(self, refusal)
                return cleaned

        return RefusedForm

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """A service refusal rolls the first pass back; the second pass re-renders the bound form with the
        refusal attached, so the staff user keeps the input and never sees a success banner."""
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ServiceRefused as exc:
            for _name, uploads in request.FILES.lists():
                for upload in uploads:
                    upload.seek(0)  # the first pass read the uploads (image processing)
            token = _REFUSAL.set(exc.error)
            try:
                return super().changeform_view(request, object_id, form_url, extra_context)
            finally:
                _REFUSAL.reset(token)

    def delete_view(
        self, request: HttpRequest, object_id: str, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        try:
            return super().delete_view(request, object_id, extra_context)
        except ServiceRefused as exc:
            for message in exc.error.messages:
                messages.error(request, message)
            meta = self.model._meta
            return HttpResponseRedirect(
                reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[object_id])
            )

    def save_model(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        try:
            services.save_row(obj, actor=request.user, request=request)
        except ValidationError as exc:  # the ModelForm validated already; this is the last line of defence
            raise ServiceRefused(exc) from exc

    def delete_model(self, request: HttpRequest, obj: models.Model) -> None:
        try:
            services.delete_row(obj, actor=request.user, request=request)
        except ValidationError as exc:
            raise ServiceRefused(exc) from exc

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: bool) -> None:
        # Inline rows (weekly hours of a professional) pass through the same services, one by one.
        try:
            for obj in formset.save(commit=False):
                services.save_row(obj, actor=request.user, request=request)
            for obj in formset.deleted_objects:
                services.delete_row(obj, actor=request.user, request=request)
        except ValidationError as exc:
            raise ServiceRefused(exc) from exc
        formset.save_m2m()


class WorkingHoursInline(OpsTabularInline):
    model = WorkingHours
    extra = 0
    fields = ("weekday", "start", "end", "break_start", "break_end")

    def formfield_for_dbfield(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        if getattr(db_field, "null", False) and not db_field.blank:
            kwargs.setdefault("required", False)
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
