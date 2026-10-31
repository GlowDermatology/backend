"""Read-only Django admin (the Unfold "Glow Derma Ops" console, ``glowderma.core.admin_site``).

The Django admin is an operator's inspection window, never a write path: every registration goes
through ``ReadOnlyAdmin`` (or ``register_read_only``), which lets active ``is_staff`` users view
records and refuses add, change and delete.

The column helpers below (:func:`status_column`, :func:`money_column`, :func:`masked_phone_column`, …) build
``list_display`` callables with Unfold's ``display`` attributes, so every module renders statuses, so‘m
amounts and personal data the same way.
"""

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from django.contrib import admin
from django.db import models
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import TabularInline as UnfoldTabularInline
from unfold.contrib.filters.admin import (
    ChoicesDropdownFilter,
    RangeDateTimeFilter,
)
from unfold.decorators import display as unfold_display
from unfold.utils import display_for_label

from glowderma.core.i18n import format_number
from glowderma.core.models import AuditEntry, IdempotencyRecord, OutboxMessage
from glowderma.core.pii import mask_email, mask_phone

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[models.Model]
    _TabularInline = admin.TabularInline[models.Model, models.Model]
else:  # the runtime classes are Unfold's (not generic, untyped)
    _ModelAdmin = UnfoldModelAdmin
    _TabularInline = UnfoldTabularInline

# Public bases for the few editable admins (professionals): Unfold at runtime, Django's types for mypy.
OpsModelAdmin = _ModelAdmin
OpsTabularInline = _TabularInline

_F = TypeVar("_F", bound=Callable[..., Any])
# A ``list_display`` callable built below (its return shape depends on the Unfold display mode).
Column = Callable[[models.Model], Any]

# Unfold label variants (``unfold/helpers/label.html``).
SUCCESS, WARNING, DANGER, INFO, PRIMARY = "success", "warning", "danger", "info", "primary"


def display(**options: Any) -> Callable[[_F], _F]:
    """Typed wrapper of :func:`unfold.decorators.display` (``label=``, ``header=``, ``ordering=``, …)."""
    return cast(Callable[[_F], _F], unfold_display(**options))


def som(value: int | None) -> str:
    """An integer so‘m amount as the admin panel shows it: ``1 250 000 so‘m`` (NBSP groups)."""
    return "-" if value is None else f"{format_number(int(value), 'uz')} so‘m"


def _resolve(obj: Any, path: str) -> Any:
    for part in path.split("__"):
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    return obj


def money_column(path: str, description: str) -> Column:
    """A ``list_display`` column rendering the so‘m amount at ``path`` (``total``, ``payment__amount``)."""

    @display(description=description, ordering=path)
    def column(obj: models.Model) -> str:
        return som(_resolve(obj, path))

    column.__name__ = f"{path}_som"
    column.source_field = path  # type: ignore[attr-defined]
    return column


def status_column(
    path: str,
    description: str,
    variants: Mapping[str, str],
    labels: Mapping[str, str] | None = None,
) -> Column:
    """A coloured status label: ``variants`` maps a value to an Unfold variant (``success``, ``danger``, …),
    ``labels`` to its Uzbek text (default: the ``choices`` label, else the raw value)."""

    @display(description=description, ordering=path, label=dict(variants))
    def column(obj: models.Model) -> tuple[str, str]:
        value = _resolve(obj, path)
        if value in (None, ""):
            return ("", "-")
        text = (labels or {}).get(value)
        if text is None:
            getter = getattr(obj, f"get_{path}_display", None) if "__" not in path else None
            text = str(getter()) if callable(getter) else str(value)
        return (str(value), text)

    column.__name__ = f"{path}_label"
    column.source_field = path  # type: ignore[attr-defined]
    return column


def status_badge(
    path: str,
    description: str,
    variants: Mapping[str, str],
    labels: Mapping[str, str] | None = None,
) -> Column:
    """:func:`status_column` rendered as ready HTML, for read-only rows (inlines, change pages) where Unfold
    does not apply ``display(label=…)``. Use it as ``name = staticmethod(status_badge(...))``."""
    base = status_column(path, description, variants, labels)

    @display(description=description)
    def column(obj: models.Model) -> SafeString | str:
        value, text = base(obj)
        if not value:
            return "-"
        rendered: SafeString = display_for_label((value, text), "-", dict(variants))
        return rendered

    column.__name__ = f"{path}_badge"
    column.source_field = path  # type: ignore[attr-defined]
    return column


def masked_phone_column(path: str, description: str = "Telefon") -> Column:
    """``+998 90 *** ** 67``: phones never appear in full on a list page (customer contract §1.4)."""

    @display(description=description, empty_value="-")
    def column(obj: models.Model) -> str | None:
        value = _resolve(obj, path)
        return mask_phone(str(value)) if value else None

    column.__name__ = f"{path}_masked"
    column.source_field = path  # type: ignore[attr-defined]
    return column


def masked_email_column(path: str, description: str = "Email") -> Column:
    """``m***@gmail.com`` on list pages."""

    @display(description=description, empty_value="-")
    def column(obj: models.Model) -> str | None:
        value = _resolve(obj, path)
        return mask_email(str(value)) if value else None

    column.__name__ = f"{path}_masked"
    column.source_field = path  # type: ignore[attr-defined]
    return column


def customer_column(path: str = "customer", description: str = "Mijoz") -> Column:
    """Two-line customer cell: name (or ``#id``) over the masked phone, else the masked email — the full
    contact stays on the change page. Pair it with ``list_select_related``."""

    @display(description=description, header=True, ordering=path)
    def column(obj: models.Model) -> list[str]:
        customer = _resolve(obj, path)
        if customer is None:
            return ["-", ""]
        phone = getattr(customer, "phone", "") or ""
        email = getattr(customer, "email", "") or ""
        contact = mask_phone(phone) if phone else (mask_email(email) if email else "")
        name = getattr(customer, "name", "") or f"#{customer.pk}"
        return [str(name), contact]

    column.__name__ = f"{path}_card"
    column.source_field = path  # type: ignore[attr-defined]
    return column


def column_field(entry: Any) -> str:
    """The model field behind a ``list_display`` / ``list_filter`` entry: a plain name, a column built by the
    helpers above (``source_field``), a ``(field, FilterClass)`` pair or another callable's name."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, tuple):
        return str(entry[0])
    return str(getattr(entry, "source_field", getattr(entry, "__name__", entry)))


def dropdown(field: str) -> tuple[str, type[ChoicesDropdownFilter]]:
    """``list_filter`` entry: a choices field as an Unfold dropdown."""
    return (field, ChoicesDropdownFilter)


def date_range(field: str) -> tuple[str, type[RangeDateTimeFilter]]:
    """``list_filter`` entry: a datetime field as an Unfold from/to range."""
    return (field, RangeDateTimeFilter)


def _json_renderer(field: models.Field[Any, Any]) -> Column:
    def render(obj: models.Model) -> str:
        value = getattr(obj, field.name)
        if value is None:
            return "-"
        text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        rendered: SafeString = format_html('<pre class="gd-json">{}</pre>', text)
        return rendered

    render.__name__ = f"{field.name}_pretty"
    render.short_description = field.verbose_name  # type: ignore[attr-defined]
    return render


def is_active_staff(request: HttpRequest) -> bool:
    """The one predicate behind every view grant of the read-only admin (and its inlines)."""
    user = request.user
    return bool(user.is_active and user.is_staff)


_is_active_staff = is_active_staff


class ReadOnlyTabularInline(_TabularInline):
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
    # Unfold: filters apply on "Apply" (one query, not one per click); no "add" link even for a view grant.
    list_filter_submit = True
    show_add_link = False
    # Optional change-page tabs: ``(("Tab name", ("field", "json_field_pretty", …)), …)``. Every readonly
    # field not named lands on a last "Boshqa" tab, so a new column is never silently hidden.
    tab_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()

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

    def get_fieldsets(self, request: HttpRequest, obj: models.Model | None = None) -> Any:
        if not self.tab_groups:
            return super().get_fieldsets(request, obj)
        available = self.get_readonly_fields(request, obj)
        listed: set[str] = set()
        fieldsets: list[tuple[str, dict[str, Any]]] = []
        for name, fields in self.tab_groups:
            present = [field for field in fields if field in available]
            listed.update(present)
            if present:
                fieldsets.append((name, {"fields": present, "classes": ["tab"]}))
        rest = [field for field in available if field not in listed]
        if rest:
            fieldsets.append(("Boshqa", {"fields": rest, "classes": ["tab"]}))
        return fieldsets


def register_read_only(
    model: type[models.Model], *, site: admin.AdminSite = admin.site, **options: Any
) -> None:
    """Register ``model`` on ``site`` with a ``ReadOnlyAdmin`` subclass built from ``options``.

    ``options`` are ``ModelAdmin`` attributes (``list_display``, ``search_fields``, ``exclude``, ...).
    """
    site.register(model, ReadOnlyAdmin, **options)


OUTBOX_STATUS_VARIANTS = {
    "pending": INFO,
    "in_flight": PRIMARY,
    "done": SUCCESS,
    "failed": WARNING,
    "dead": DANGER,
}

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
    list_filter=(dropdown("actor_type"), "action", "entity_type", date_range("created_at")),
    search_fields=("actor_label", "action", "entity_type", "entity_id", "request_id", "ip"),
    ordering=("-id",),
    date_hierarchy="created_at",
)
register_read_only(
    OutboxMessage,
    list_display=(
        "id",
        "topic",
        "dedupe_key",
        status_column("status", "Holat", OUTBOX_STATUS_VARIANTS),
        "attempts",
        "available_at",
        "updated_at",
    ),
    list_filter=(dropdown("status"), "topic", date_range("updated_at")),
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
