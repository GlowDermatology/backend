"""Read-only Django admin.

The Django admin is an operator's inspection window, never a write path: every registration goes
through ``ReadOnlyAdmin`` (or ``register_read_only``), which lets active ``is_staff`` users view
records and refuses add, change and delete.
"""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.db import models
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString

from glowderma.core.models import AuditEntry, IdempotencyRecord, OutboxMessage

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[models.Model]
else:
    _ModelAdmin = admin.ModelAdmin  # the runtime class is not generic


def _json_renderer(field: models.Field[Any, Any]) -> Callable[[models.Model], str]:
    def render(obj: models.Model) -> str:
        value = getattr(obj, field.name)
        if value is None:
            return "-"
        text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        rendered: SafeString = format_html("<pre>{}</pre>", text)
        return rendered

    render.__name__ = f"{field.name}_pretty"
    render.short_description = field.verbose_name  # type: ignore[attr-defined]
    return render


def is_active_staff(request: HttpRequest) -> bool:
    """The one predicate behind every view grant of the read-only admin (and its inlines)."""
    user = request.user
    return bool(user.is_active and user.is_staff)


_is_active_staff = is_active_staff


class ReadOnlyTabularInline(admin.TabularInline):  # type: ignore[type-arg]
    """View-only inline: the same grant as ``ReadOnlyAdmin``, never a write path."""

    can_delete = False
    extra = 0
    show_change_link = True

    def has_add_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_view_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return is_active_staff(request)

    def get_readonly_fields(self, request: HttpRequest, obj: models.Model | None = None) -> list[str]:
        # Exactly the declared ``fields`` (flat; computed columns such as ``line_total`` included), else every
        # concrete field — so the inline never builds an editable form field.
        if self.fields:
            names = [name for name in self.fields if isinstance(name, str)]
        else:
            names = [field.name for field in self.model._meta.concrete_fields]
        names.extend(name for name in self.readonly_fields if name not in names)
        return names


class ReadOnlyAdmin(_ModelAdmin):
    """View-only ``ModelAdmin``: JSON fields render as preformatted text, nothing can be written.

    Large tables (ledger, events, inbox) are the norm, so pages are short and the changelist never runs the
    full ``COUNT(*)``.
    """

    actions = None
    list_per_page = 50
    show_full_result_count = False

    def __init__(self, model: type[models.Model], admin_site: admin.AdminSite) -> None:
        super().__init__(model, admin_site)
        for field in model._meta.concrete_fields:
            if isinstance(field, models.JSONField):
                setattr(self, f"{field.name}_pretty", _json_renderer(field))

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_view_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return _is_active_staff(request)

    def has_module_permission(self, request: HttpRequest) -> bool:
        # Same predicate as the view grant: otherwise the app index and the index links stay hidden
        # from exactly the users who may view the records.
        return _is_active_staff(request)

    def _json_field_names(self) -> list[str]:
        return [f.name for f in self.model._meta.concrete_fields if isinstance(f, models.JSONField)]

    def get_exclude(self, request: HttpRequest, obj: models.Model | None = None) -> list[str]:
        # JSON fields leave the form entirely: they appear once, through their ``<name>_pretty`` renderer.
        return [*(self.exclude or ()), *self._json_field_names()]

    def get_readonly_fields(self, request: HttpRequest, obj: models.Model | None = None) -> list[str]:
        excluded = set(self.exclude or ())
        names: list[str] = []
        for field in self.model._meta.concrete_fields:
            if field.name in excluded:
                continue
            names.append(f"{field.name}_pretty" if isinstance(field, models.JSONField) else field.name)
        names.extend(name for name in self.readonly_fields if name not in names)
        return names


def register_read_only(
    model: type[models.Model], *, site: admin.AdminSite = admin.site, **options: Any
) -> None:
    """Register ``model`` on ``site`` with a ``ReadOnlyAdmin`` subclass built from ``options``.

    ``options`` are ``ModelAdmin`` attributes (``list_display``, ``search_fields``, ``exclude``, ...).
    """
    site.register(model, ReadOnlyAdmin, **options)


register_read_only(
    AuditEntry,
    list_display=(
        "id",
        "created_at",
        "actor_type",
        "actor_label",
        "action",
        "entity_type",
        "entity_id",
        "ip",
    ),
    list_filter=("actor_type", "action", "entity_type"),
    search_fields=("actor_label", "action", "entity_type", "entity_id", "request_id", "ip"),
    ordering=("-id",),
    date_hierarchy="created_at",
)
register_read_only(
    OutboxMessage,
    list_display=("id", "topic", "dedupe_key", "status", "attempts", "available_at", "updated_at"),
    list_filter=("status", "topic"),
    search_fields=("topic", "dedupe_key", "last_error"),
    ordering=("-id",),
)
register_read_only(
    IdempotencyRecord,
    # the body hash and the stored answer stay out of the admin: bodies can carry passwords, answers tokens
    exclude=("request_hash", "response_body"),
    list_display=("id", "audience", "actor_id", "method", "path", "key", "response_status", "expires_at"),
    list_filter=("audience", "method", "response_status"),
    search_fields=("path", "key", "actor_id"),
    ordering=("-id",),
)
