"""Read-only Django admin (the Unfold "Glow Derma Ops" console, ``glowderma.core.admin_site``).

The Django admin is an operator's inspection window, never a write path: every registration goes
through ``ReadOnlyAdmin`` (or ``register_read_only``), which lets active ``is_staff`` users view
records and refuses add, change and delete.

The column helpers below (:func:`status_column`, :func:`money_column`, :func:`masked_phone_column`, …) build
``list_display`` callables with Unfold's ``display`` attributes, so every module renders statuses, so‘m
amounts and personal data the same way.
"""

import json
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from django import forms
from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import Count
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.utils.text import capfirst
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import TabularInline as UnfoldTabularInline
from unfold.contrib.filters.admin import (
    ChoicesDropdownFilter,
    RangeDateTimeFilter,
)
from unfold.decorators import display as unfold_display
from unfold.utils import display_for_label

from glowderma.core import consistency, outbox, timeutils
from glowderma.core.admin_actions import (
    ActionResult,
    ActionVariant,
    ConfirmForm,
    OpsActionsMixin,
    done,
    enqueue_task,
    failed,
    ops_action,
    refused,
)
from glowderma.core.i18n import format_number
from glowderma.core.models import AuditEntry, IdempotencyRecord, OutboxMessage
from glowderma.core.pii import mask_email, mask_phone

logger = logging.getLogger(__name__)

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


class TitledObject:
    """``obj`` with a readable ``str()``: the change page's ``original`` (every attribute is ``obj``'s)."""

    def __init__(self, obj: models.Model, title: str) -> None:
        self._obj = obj
        self._title = title

    def __getattr__(self, name: str) -> Any:
        return getattr(self._obj, name)

    def __str__(self) -> str:
        return self._title


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
    # The change page's heading, breadcrumb and browser title (:meth:`object_title`): a ``str.format``
    # pattern over ``obj`` (``"Buyurtma №{obj.pk}"``); ``None`` = "<Verbose name> #<pk>". The model's
    # ``__str__`` (English, asserted by tests, used in logs) is never shown as the page title.
    title_format: str | None = None

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

    def object_title(self, obj: models.Model) -> str:
        """A readable Uzbek title of ``obj`` for its change page (``title_format``)."""
        if self.title_format is not None:
            return self.title_format.format(obj=obj)
        return f"{capfirst(str(self.model._meta.verbose_name))} #{obj.pk}"

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> Any:
        obj = self.get_object(request, unquote(object_id)) if object_id else None
        if obj is not None:
            title = self.object_title(obj)
            extra_context = {
                **(extra_context or {}),
                # Unfold's header breadcrumb renders ``original``; the proxy keeps pk/_meta for its links.
                "original": TitledObject(obj, title),
                "title": capfirst(str(self.model._meta.verbose_name)),
                "subtitle": title,
            }
        return super().changeform_view(request, object_id, form_url, extra_context)

    def history_view(
        self, request: HttpRequest, object_id: str, extra_context: dict[str, Any] | None = None
    ) -> Any:
        obj = self.get_object(request, unquote(object_id))
        if obj is not None:
            extra_context = {**(extra_context or {}), "title": f"Tarix: {self.object_title(obj)}"}
        return super().history_view(request, object_id, extra_context)

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

# --- safe audited actions (glowderma.core.admin_actions) -------------------------------------------------
REQUEUE_REASON = "ops console"
# One click of "requeue every dead message of a topic" handles at most this many (the rest: click again).
TOPIC_REQUEUE_LIMIT = 200
# "Run the consistency report now": once per this many seconds, whoever clicks (the report reads every
# ledger entry and may alert).
CONSISTENCY_REPORT_COOLDOWN = 600
CONSISTENCY_COOLDOWN_KEY = "ops:consistency_report"
CONSISTENCY_TASK = "reporting.consistency_report"


def _requeue_refusal(model_admin: Any, request: HttpRequest, message: OutboxMessage) -> str:
    if message.status == "dead" or consistency.is_overdue(message, timeutils.now()):
        return ""
    if message.status == "done":
        return "Xabar allaqachon yetkazilgan: qayta navbatga qo‘yish kerak emas."
    return "Xabar navbatda va muddati o‘tmagan: dispetcher uni o‘zi yetkazadi."


def _requeue_notes(model_admin: Any, request: HttpRequest, message: OutboxMessage) -> list[str]:
    hooks = outbox.requeue_hooks(message.topic)
    notes = [f"Topic: {message.topic} · urinishlar: {message.attempts}"]
    about = (hooks.describe_uz or hooks.describe)(message) if hooks is not None else ""
    if about:
        notes.append(f"Tafsilot: {about}")
    warning = (hooks.warning_uz or hooks.warning) if hooks is not None else ""
    if message.status == "dead" and warning:
        notes.append(f"Ogohlantirish: {warning}")
    return notes


def _dead_topics() -> list[tuple[str, int]]:
    rows = (
        OutboxMessage.objects.filter(status="dead")
        .values("topic")
        .annotate(total=Count("id"))
        .order_by("topic")
    )
    return [(str(row["topic"]), int(row["total"])) for row in rows]


def _no_dead_messages(model_admin: Any, request: HttpRequest, obj: None) -> str:
    return "" if OutboxMessage.objects.filter(status="dead").exists() else "O‘lik xabarlar yo‘q."


def _topic_warnings(model_admin: Any, request: HttpRequest, obj: None) -> list[str]:
    notes = []
    for topic, _total in _dead_topics():
        hooks = outbox.requeue_hooks(topic)
        warning = (hooks.warning_uz or hooks.warning) if hooks is not None else ""
        if warning:
            notes.append(f"Ogohlantirish ({topic}): {warning}")
    return notes


class DeadTopicForm(ConfirmForm):
    topic = forms.ChoiceField(label="Topic")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = cast(forms.ChoiceField, self.fields["topic"])
        field.choices = [(topic, f"{topic} — {total} ta o‘lik") for topic, total in _dead_topics()]


def row_action_link(url: str, label: str, icon: str) -> SafeString:
    """A changelist cell button that opens an ops action's confirm step in Unfold's modal (only rendered for
    the rows the action makes sense for; the URL is the action's change-page route)."""
    rendered: SafeString = format_html(
        '<a href="{0}" hx-get="{0}" hx-target="#modal-content" hx-select="#dialog" '
        'x-on:htmx:after-on-load="openModal = true" class="gd-row-action">'
        '<span class="material-symbols-outlined">{2}</span>{1}</a>',
        url,
        label,
        icon,
    )
    return rendered


class OutboxMessageAdmin(OpsActionsMixin, ReadOnlyAdmin):
    list_display = (
        "id",
        "topic",
        "dedupe_key",
        status_column("status", "Holat", OUTBOX_STATUS_VARIANTS),
        "attempts",
        "available_at",
        "updated_at",
        "requeue_button",
    )
    list_filter = (dropdown("status"), "topic", date_range("updated_at"))
    search_fields = ("topic", "dedupe_key", "last_error")
    ordering = ("-id",)
    actions_detail = ("requeue",)
    actions_list = ("requeue_dead_topic",)

    @display(description="Amal")
    def requeue_button(self, message: OutboxMessage) -> SafeString | str:
        if _requeue_refusal(self, cast(HttpRequest, None), message):
            return ""
        url = reverse("admin:core_outboxmessage_requeue", args=[message.pk])
        back = reverse("admin:core_outboxmessage_changelist")
        return row_action_link(f"{url}?next={back}", "Qayta navbatga", "replay")

    @ops_action(
        description="Qayta navbatga qo‘yish",
        title="Xabarni qayta navbatga qo‘yish",
        body=(
            "O‘lik xabar yana «pending» holatiga qaytadi (urinishlar 0 dan boshlanadi) va dispetcher uni "
            "darhol yetkazishga urinadi. Muddati o‘tgan xabar uchun dispetcher qayta ishga tushiriladi. "
            "Ishlovchilar idempotent: muvaffaqiyatli bajarilgan amal takrorlanmaydi."
        ),
        submit="Qayta navbatga qo‘yish",
        icon="replay",
        available=_requeue_refusal,
        notes=_requeue_notes,
    )
    def requeue(self, request: HttpRequest, message: OutboxMessage, form: ConfirmForm) -> ActionResult:
        if message.status == "dead":
            result = outbox.requeue_dead(
                message.pk, reason=REQUEUE_REASON, actor=request.user, request=request
            )
            if result.outcome == "skipped":
                hooks = outbox.requeue_hooks(message.topic)
                why = hooks.skip_reason_uz(message) if hooks is not None and hooks.skip_reason_uz else ""
                return refused(f"Xabar #{message.pk} qayta navbatga qo‘yilmadi: {why or result.reason}")
            if result.outcome != "requeued":
                return refused(f"Xabar #{message.pk} endi o‘lik emas: hech narsa o‘zgarmadi.")
            return done(
                f"Xabar #{message.pk} qayta navbatga qo‘yildi.",
                audit_action="ops.outbox_requeued",
                entity_type="outbox",
                entity_id=message.pk,
                before={"status": "dead", "attempts": message.attempts},
                after={"topic": message.topic, "status": "pending"},
            )
        from glowderma.core.tasks import dispatch_outbox

        transaction.on_commit(dispatch_outbox.delay, robust=True)
        return done(
            f"Xabar #{message.pk} navbatda turibdi; dispetcher qayta ishga tushirildi.",
            audit_action="ops.outbox_dispatch_kicked",
            entity_type="outbox",
            entity_id=message.pk,
            before={"status": message.status, "available_at": message.available_at},
            after={"topic": message.topic},
        )

    @ops_action(
        description="Topic bo‘yicha o‘liklarni qayta navbatga",
        title="Topicning barcha o‘lik xabarlarini qayta navbatga qo‘yish",
        body=(
            f"Tanlangan topicning o‘lik xabarlari (bir bosishda {TOPIC_REQUEUE_LIMIT} tagacha) qayta "
            "navbatga qo‘yiladi. Ishi boshqa yo‘l bilan bajarilgan xabarlar o‘tkazib yuboriladi."
        ),
        submit="Hammasini qayta navbatga",
        icon="restart_alt",
        variant=ActionVariant.DANGER,
        manager_only=True,
        form_class=DeadTopicForm,
        url_path="requeue-dead-topic",
        available=_no_dead_messages,
        notes=_topic_warnings,
    )
    def requeue_dead_topic(self, request: HttpRequest, obj: None, form: ConfirmForm) -> ActionResult:
        topic = str(form.cleaned_data["topic"])
        ids = list(
            OutboxMessage.objects.filter(topic=topic, status="dead")
            .order_by("id")
            .values_list("pk", flat=True)[: TOPIC_REQUEUE_LIMIT + 1]
        )
        more = len(ids) > TOPIC_REQUEUE_LIMIT
        requeued: list[int] = []
        skipped: list[int] = []
        for message_id in ids[:TOPIC_REQUEUE_LIMIT]:
            result = outbox.requeue_dead(
                message_id, reason=REQUEUE_REASON, actor=request.user, request=request
            )
            if result.outcome == "requeued":
                requeued.append(message_id)
            elif result.outcome == "skipped":
                skipped.append(message_id)
        if not requeued:
            return refused(
                f"{topic}: qayta navbatga qo‘yiladigan xabar topilmadi ({len(skipped)} ta o‘tkazildi)."
            )
        tail = " Yana o‘lik xabarlar qoldi — amalni takrorlang." if more else ""
        return done(
            f"{topic}: {len(requeued)} ta xabar qayta navbatga qo‘yildi, {len(skipped)} ta o‘tkazib "
            f"yuborildi.{tail}",
            audit_action="ops.outbox_topic_requeued",
            entity_type="outbox_topic",
            entity_id=topic,
            after={"requeued": requeued, "skipped": skipped, "more": more},
        )


class AuditEntryAdmin(OpsActionsMixin, ReadOnlyAdmin):
    list_display = (
        "id",
        "created_at",
        "actor_type",
        "actor_label",
        "action",
        "entity_type",
        "entity_id",
        "ip",
    )
    list_filter = (dropdown("actor_type"), "action", "entity_type", date_range("created_at"))
    search_fields = ("actor_label", "action", "entity_type", "entity_id", "request_id", "ip")
    ordering = ("-id",)
    date_hierarchy = "created_at"
    actions_list = ("run_consistency_report",)

    @ops_action(
        description="Hisobotni hozir ishga tushirish",
        title="Moliyaviy muvofiqlik hisobotini hozir ishga tushirish",
        body=(
            "K1–K6 tekshiruvlari va cashback daftari solishtiruvi navbatga qo‘yiladi. Natija audit "
            "jurnaliga «report.consistency» yozuvi bo‘lib tushadi; nomuvofiqlik bo‘lsa, ogohlantirish "
            "yuboriladi. Hisobot 10 daqiqada bir martadan ko‘p ishga tushirilmaydi."
        ),
        submit="Ishga tushirish",
        icon="fact_check",
        manager_only=True,
        url_path="run-consistency-report",
    )
    def run_consistency_report(self, request: HttpRequest, obj: None, form: ConfirmForm) -> ActionResult:
        try:
            claimed = cache.add(CONSISTENCY_COOLDOWN_KEY, 1, timeout=CONSISTENCY_REPORT_COOLDOWN)
        except Exception:
            logger.exception("ops.consistency_report cooldown unavailable")
            return failed("Kesh ishlamayapti: hisobotni hozir ishga tushirib bo‘lmaydi.")
        if not claimed:
            return refused("Hisobot yaqinda ishga tushirilgan: 10 daqiqadan keyin qayta urinib ko‘ring.")
        try:
            enqueue_task(CONSISTENCY_TASK)
        except Exception:
            cache.delete(CONSISTENCY_COOLDOWN_KEY)
            raise
        return done(
            "Muvofiqlik hisoboti navbatga qo‘yildi. Natija audit jurnalida «report.consistency» bo‘lib "
            "chiqadi.",
            audit_action="ops.consistency_report_requested",
            entity_type="report",
            entity_id="consistency",
        )


admin.site.register(AuditEntry, AuditEntryAdmin)
admin.site.register(OutboxMessage, OutboxMessageAdmin)
register_read_only(
    IdempotencyRecord,
    # the body hash and the stored answer stay out of the admin: bodies can carry passwords, answers tokens
    exclude=("request_hash", "response_body"),
    list_display=("id", "audience", "actor_id", "method", "path", "key", "response_status", "expires_at"),
    list_filter=("audience", "method", "response_status"),
    search_fields=("path", "key", "actor_id"),
    ordering=("-id",),
)
