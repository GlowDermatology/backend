"""Django admin of the delivery models (design 02 §6; Yandex Delivery plan §3.10).

Everything is read-only (``ReadOnlyAdmin``) except the delivery settings page: the warehouse, its opening
hours and special days, the methods and the pricing policy (``DeliverySettings`` with the ``WarehouseHours`` /
``WarehouseClosure`` inlines), which a ``ServiceBackedAdmin`` writes through the audited
``delivery.services`` (``update_settings``, ``save_hours`` / ``delete_hours``, ``save_closure`` /
``delete_closure``). The page's «Yandex bilan tekshirish» action asks Yandex which classes the warehouse point
offers (``services.check_yandex_tariffs``) outside any transaction.

No page shows a courier's phone (none is stored); quotes list no address or pin columns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.utils.translation import gettext, gettext_lazy
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminTimeWidget

from glowderma.core import enums, labels, timeutils
from glowderma.core.admin import (
    DANGER,
    INFO,
    PRIMARY,
    SUCCESS,
    WARNING,
    ReadOnlyAdmin,
    ReadOnlyTabularInline,
    ServiceBackedAdmin,
    date_range,
    display,
    dropdown,
    is_active_staff,
    money_column,
    number_link_column,
    register_read_only,
    status_column,
)
from glowderma.core.admin_actions import ConfirmForm, OpsActionsMixin, ops_action
from glowderma.delivery import services
from glowderma.delivery.models import (
    CourierClaim,
    DeliveryIssue,
    DeliveryQuote,
    DeliverySettings,
    Shipment,
    ShipmentEvent,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.integrations import selectors as integration_selectors

SHIPMENT_STATE_VARIANTS = {"not_created": WARNING, "failed": DANGER, "created": SUCCESS}
# ``Shipment.bts_status`` / ``ShipmentEvent.bts_status``: the port's status vocabulary
# (glowderma.integrations.bts.protocol.port_status) — BTS's own codes are translated to these three, anything
# else is recorded as BTS sent it.
BTS_STATUS_LABELS = {
    "100": _("Qabul qilindi (bekor qilish mumkin)"),
    "200": _("Kuryerda / yo‘lda"),
    "300": _("Yetkazildi"),
    "1300": _("BTS’da o‘chirilgan"),
    "1400": _("Muddati o‘tgan"),
    "unknown": _("Noma’lum holat"),
}
PROVIDER_VARIANTS: dict[str, str] = {enums.DeliveryProvider.BTS: INFO, enums.DeliveryProvider.YANDEX: PRIMARY}
STAGE_VARIANTS: dict[str, str] = {
    enums.ShipmentStage.AWAITING_DISPATCH: WARNING,
    enums.ShipmentStage.COURIER_REQUESTED: INFO,
    enums.ShipmentStage.COURIER_ASSIGNED: INFO,
    enums.ShipmentStage.COURIER_ARRIVED: INFO,
    enums.ShipmentStage.IN_TRANSIT: PRIMARY,
    enums.ShipmentStage.AT_DESTINATION: PRIMARY,
    enums.ShipmentStage.DELIVERED: SUCCESS,
    enums.ShipmentStage.RETURNING: WARNING,
    enums.ShipmentStage.RETURNED: WARNING,
    enums.ShipmentStage.CANCELLED: DANGER,
    enums.ShipmentStage.FAILED: DANGER,
}
ISSUE_STATE_VARIANTS: dict[str, str] = {
    enums.DeliveryIssueState.OPEN: WARNING,
    enums.DeliveryIssueState.RESOLVED: SUCCESS,
}


def bts_status_text(value: str) -> str:
    """``Yetkazildi (300)``: the meaning in the console's language with the code BTS sent; ``-`` for none."""
    if not value:
        return "-"
    lazy_label = BTS_STATUS_LABELS.get(value)
    label = "" if lazy_label is None else str(lazy_label)
    return f"{label} ({value})" if label and value != "unknown" else (label or value)


@display(description=_("BTS holati"), ordering="bts_status")
def bts_status(obj: models.Model) -> str:
    return bts_status_text(str(getattr(obj, "bts_status", "")))


SHIPMENT_EVENT_VARIANTS = {
    "created": SUCCESS,
    "create_failed": DANGER,
    "cancel_refused": WARNING,
}


class ShipmentEventInline(ReadOnlyTabularInline):
    model = ShipmentEvent
    fields = ("at", "kind", "bts_status_label", "carrier_status", "via", "manual", "error_code")
    bts_status_label = staticmethod(bts_status)
    ordering = ("at", "id")
    tab = True
    verbose_name_plural = _("Hodisalar")


class CourierClaimInline(ReadOnlyTabularInline):
    """The Yandex requests of a shipment (one per «Kuryer chaqirish»)."""

    model = CourierClaim
    fields = ("number", "status", "taxi_class", "offer_price", "final_price", "accepted_at", "ended_at")
    ordering = ("number",)
    tab = True
    verbose_name_plural = _("Kuryer buyurtmalari")


class ShipmentAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        number_link_column("order", _("Buyurtma")),
        status_column("provider", _("Yetkazib beruvchi"), PROVIDER_VARIANTS),
        status_column("state", _("Holat"), SHIPMENT_STATE_VARIANTS),
        status_column("stage", _("Bosqich"), STAGE_VARIANTS),
        "method",
        "tracking_number",
        money_column("tariff", _("Tarif")),
        bts_status,
        "via",
        "attempts",
        "last_error_code",
        "last_attempt_at",
    )
    exclude = ("bts_status",)
    readonly_fields = ("bts_status_label",)
    bts_status_label = staticmethod(bts_status)
    list_filter = (
        dropdown("provider"),
        dropdown("state"),
        dropdown("stage"),
        dropdown("via"),
        date_range("last_attempt_at"),
    )
    search_fields = ("tracking_number", "=order__id")
    ordering = ("-id",)
    autocomplete_fields = ("order", "create_message")
    raw_id_fields = ("quote", "claim", "prepared_by", "return_confirmed_by")  # Yandex Delivery
    money_fields = frozenset({"tariff", "carrier_cost_total", "cost_difference"})
    inlines = (ShipmentEventInline, CourierClaimInline)


admin.site.register(Shipment, ShipmentAdmin)
register_read_only(
    ShipmentEvent,
    list_display=(
        "at",
        number_link_column("shipment", _("Jo‘natma"), prefix="#"),
        status_column("kind", _("Hodisa"), SHIPMENT_EVENT_VARIANTS),
        bts_status,
        "carrier_status",
        "via",
        "error_code",
    ),
    list_filter=(dropdown("kind"), dropdown("via"), date_range("at")),
    search_fields=("=shipment__id",),
    ordering=("-at", "-id"),
    exclude=("bts_status",),
    readonly_fields=("bts_status_label",),
    bts_status_label=staticmethod(bts_status),
    autocomplete_fields=("shipment",),
    date_hierarchy="at",
)


# --- Yandex Delivery: claims, quotes, the exception queue (read-only) --------------------------------------


class CourierClaimAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        number_link_column("shipment", _("Jo‘natma"), prefix="#"),
        "number",
        "claim_id",
        "status",
        "taxi_class",
        "method",
        money_column("offer_price", _("Taklif narxi")),
        money_column("final_price", _("Yakuniy narx")),
        "accepted_at",
        "ended_at",
    )
    list_filter = (dropdown("method"), date_range("created_at"))
    search_fields = ("claim_id", "=shipment__order__id")
    ordering = ("-id",)
    raw_id_fields = ("shipment", "requested_by")
    money_fields = frozenset(
        {"quoted_fee", "estimate_price", "offer_price", "approved_price", "final_price", "cancel_fee"}
    )
    title_format = _("Kuryer buyurtmasi #{obj.pk}")


class DeliveryIssueAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        number_link_column("order", _("Buyurtma")),
        "kind",
        "code",
        "urgent",
        status_column("state", _("Holat"), ISSUE_STATE_VARIANTS),
        "opened_at",
        "updated_at",
        "reminders",
        "resolved_at",
    )
    list_filter = (dropdown("state"), dropdown("kind"), "urgent", date_range("opened_at"))
    search_fields = ("=order__id", "code")
    ordering = ("state", "-urgent", "opened_at", "id")
    raw_id_fields = ("order", "resolved_by", "created_by")
    title_format = _("Yetkazish muammosi #{obj.pk}")


class DeliveryQuoteAdmin(ReadOnlyAdmin):
    """Checkout quotes: the amounts and the window; the address snapshot stays on the change page only."""

    list_display = (
        "id",
        number_link_column("customer", _("Mijoz"), prefix="#"),
        "provider",
        "method",
        money_column("customer_fee", _("Mijoz to‘lovi")),
        money_column("carrier_price", _("Yetkazib beruvchi narxi")),
        "weight_g",
        "created_at",
        "expires_at",
        "used_at",
        number_link_column("order", _("Buyurtma")),
    )
    list_filter = (dropdown("provider"), dropdown("method"), date_range("created_at"))
    search_fields = ("=order__id", "=customer__id")
    ordering = ("-created_at",)
    raw_id_fields = ("customer", "order")
    exclude = ("latitude", "longitude")
    money_fields = frozenset({"customer_fee", "carrier_price"})
    title_format = _("Narx taklifi {obj.pk}")


admin.site.register(CourierClaim, CourierClaimAdmin)
admin.site.register(DeliveryIssue, DeliveryIssueAdmin)
admin.site.register(DeliveryQuote, DeliveryQuoteAdmin)


# --- the delivery settings page (editable, through the services) --------------------------------------------

TIME_FORMAT = "%H:%M"
if TYPE_CHECKING:
    _SettingsForm = forms.ModelForm[DeliverySettings]
    _HoursForm = forms.ModelForm[WarehouseHours]
else:  # Django's ModelForm is not subscriptable at runtime
    _SettingsForm = _HoursForm = forms.ModelForm
CALENDAR_DAYS_SHOWN = 10  # coming special days listed on the settings page
TAXI_CLASS_CHOICES = [(name, name) for name in services.METHOD_TAXI_CLASSES]
WEEKDAY_CHOICES = [(index, gettext_lazy(name)) for index, name in enumerate(labels.WEEKDAYS)]


def _attach(form: forms.BaseForm, errors: dict[str, list[tuple[Any, dict[str, Any]]]]) -> None:
    for field, entries in errors.items():
        for message, params in entries:
            text = str(message) % params if params else str(message)
            form.add_error(field if field in form.fields else None, text)


class DeliverySettingsForm(_SettingsForm):
    """The settings form: class selects, a normalised phone and the services' validation (in the viewer's
    language) before the model constraints run."""

    standard_taxi_class = forms.ChoiceField(choices=TAXI_CLASS_CHOICES, label=_("Standart: Yandex klassi"))
    express_taxi_class = forms.ChoiceField(choices=TAXI_CLASS_CHOICES, label=_("Ekspress: Yandex klassi"))
    # typed as ``+998 90 123 45 67`` and stored as ``+998901234567`` (clean_contact_phone)
    contact_phone = forms.CharField(max_length=32, required=False, label=_("Mas’ul telefoni"))

    class Meta:
        model = DeliverySettings
        fields = list(services.SETTINGS_FIELDS)

    def clean_contact_phone(self) -> str:
        return services.normalise_phone(str(self.cleaned_data.get("contact_phone") or ""))

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        stored = DeliverySettings.objects.filter(pk=self.instance.pk).first()
        previous = services.settings_snapshot(stored) if stored is not None else None
        values = {**(previous or {}), **{name: cleaned.get(name) for name in services.SETTINGS_FIELDS}}
        valid = {name: value for name, value in values.items() if name not in self.errors}
        errors = services.settings_errors(valid, previous=previous)
        _attach(self, {name: entries for name, entries in errors.items() if name not in self.errors})
        return cleaned


def _time_widgets(db_field: Any, kwargs: dict[str, Any]) -> None:
    if isinstance(db_field, models.TimeField):
        kwargs["widget"] = UnfoldAdminTimeWidget(format=TIME_FORMAT)


class WarehouseHoursForm(_HoursForm):
    weekday = forms.TypedChoiceField(choices=WEEKDAY_CHOICES, coerce=int, label=_("Hafta kuni"))

    class Meta:
        model = WarehouseHours
        fields = ("weekday", "opens_at", "closes_at")


@display(description=_("Hafta kuni"), ordering="weekday")
def weekday_label(obj: models.Model) -> str:
    weekday = getattr(obj, "weekday", None)
    if weekday is None or not 0 <= int(weekday) < len(labels.WEEKDAYS):
        return "-"
    return gettext(labels.WEEKDAYS[int(weekday)])


class WarehouseHoursAdmin(ServiceBackedAdmin):
    """The weekly opening hours (Asia/Tashkent; no row for a weekday = closed that day), written by
    ``services.save_hours`` / ``delete_hours``."""

    form = WarehouseHoursForm  # type: ignore[assignment]
    list_display = (weekday_label, "opens_at", "closes_at")
    ordering = ("weekday",)
    search_fields = ("=weekday",)

    def formfield_for_dbfield(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        _time_widgets(db_field, kwargs)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def object_title(self, obj: models.Model) -> str:
        return weekday_label(obj)

    def service_save(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        services.save_hours(cast(WarehouseHours, obj), actor=request.user, request=request)

    def service_delete(self, request: HttpRequest, obj: models.Model) -> None:
        services.delete_hours(cast(WarehouseHours, obj), actor=request.user, request=request)


class WarehouseClosureAdmin(ServiceBackedAdmin):
    """Days off and special hours (both times empty = closed), written by ``services.save_closure`` /
    ``delete_closure``."""

    fields = ("date", "opens_at", "closes_at", "note", "created_by", "created_at")
    list_display = ("date", "opens_at", "closes_at", "note", "created_by")
    ordering = ("-date",)
    search_fields = ("note",)
    date_hierarchy = "date"

    def formfield_for_dbfield(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        _time_widgets(db_field, kwargs)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def service_save(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        services.save_closure(cast(WarehouseClosure, obj), actor=request.user, request=request)

    def service_delete(self, request: HttpRequest, obj: models.Model) -> None:
        services.delete_closure(cast(WarehouseClosure, obj), actor=request.user, request=request)


admin.site.register(WarehouseHours, WarehouseHoursAdmin)
admin.site.register(WarehouseClosure, WarehouseClosureAdmin)


YANDEX_CHECK_TITLE = _("Yandex bilan tekshirish")
YANDEX_CHECK_BODY = _(
    "Yandex’dan ombor nuqtasida qaysi yetkazish klasslari (courier, express) mavjudligi so‘raladi. "
    "Kuryer chaqirilmaydi va hech narsa buyurtma qilinmaydi. Topilgan klasslar saqlanadi: ishchi rejimda "
    "usulni faqat ular bilan yoqish mumkin."
)


def _unused(model_admin: Any, request: HttpRequest, obj: Any, form: ConfirmForm) -> Any:
    raise AssertionError("the Yandex check runs outside ops_action's transaction")  # pragma: no cover


# The confirm step (GET, an invalid POST) of the Yandex check: the console's standard action page.
_yandex_check_confirm = ops_action(
    description=YANDEX_CHECK_TITLE,
    title=YANDEX_CHECK_TITLE,
    body=YANDEX_CHECK_BODY,
    submit=_("Tekshirish"),
    icon="travel_explore",
    url_path="yandex-check",
)(_unused)
_ACTION_ATTRIBUTES = (
    "allowed_permissions",
    "short_description",
    "url_path",
    "icon",
    "dialog",
    "variant",
    "extra_options",
    "attrs",
    "ops_available",
    "ops_manager_only",
)


def tariff_check_message(result: services.TariffCheck) -> tuple[int, str]:
    """The console message of a check: the classes found, or the error code (never Yandex's text)."""
    if result.ok and result.tariffs:
        return messages.SUCCESS, gettext("Yandex ombor nuqtasida taklif qiladigan klasslar: %(classes)s.") % {
            "classes": ", ".join(result.tariffs)
        }
    if result.ok:
        return messages.WARNING, gettext("Yandex ombor nuqtasida birorta ham klass taklif qilmadi.")
    if result.error_code == services.TARIFFS_NOT_CONFIGURED:
        return messages.WARNING, gettext("Avval ombor manzilini va xaritadagi nuqtasini saqlang.")
    return messages.ERROR, gettext("Yandex bilan tekshirib bo‘lmadi (kod: %(code)s).") % {
        "code": result.error_code
    }


class DeliverySettingsAdmin(OpsActionsMixin, ServiceBackedAdmin):
    """The one settings row: no add, no delete; the list opens the row itself."""

    form = DeliverySettingsForm  # type: ignore[assignment]
    list_display = ("warehouse_name", "updated_at", "updated_by")
    readonly_fields = ("updated_by", "yandex_tariffs", "calendar")
    actions_detail = ("yandex_check",)
    fieldsets = (
        (
            _("Ombor"),
            {
                "classes": ["tab"],
                "fields": (
                    "warehouse_name",
                    "calendar",
                    "warehouse_address",
                    ("warehouse_latitude", "warehouse_longitude"),
                    "contact_name",
                    "contact_phone",
                    "contact_email",
                    "courier_comment",
                ),
            },
        ),
        (
            _("Jo‘natish tartibi"),
            {
                "classes": ["tab"],
                "fields": (
                    "prep_minutes",
                    "dispatch_cutoff_minutes",
                    "prep_deadline_minutes",
                    "returns_accepted",
                    "dispatch_suspended",
                    "suspended_until",
                    "suspension_note",
                ),
            },
        ),
        (
            _("Usullar"),
            {
                "classes": ["tab"],
                "fields": (
                    "standard_enabled",
                    "standard_taxi_class",
                    ("standard_eta_min_minutes", "standard_eta_max_minutes"),
                    "express_enabled",
                    "express_taxi_class",
                    ("express_eta_min_minutes", "express_eta_max_minutes"),
                    "yandex_tariffs",
                ),
            },
        ),
        (
            _("Narx siyosati"),
            {
                "classes": ["tab"],
                "fields": (
                    "default_item_weight_g",
                    "markup_percent",
                    "estimate_vat_percent",
                    "max_auto_increase",
                    "max_loss_per_shipment",
                ),
            },
        ),
        (_("Yangilanish"), {"classes": ["tab"], "fields": ("updated_at", "updated_by")}),
    )

    # --- a singleton --------------------------------------------------------------------------------------

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def changelist_view(self, request: HttpRequest, extra_context: dict[str, Any] | None = None) -> Any:
        if not self.has_view_permission(request):
            raise PermissionDenied
        return HttpResponseRedirect(reverse("admin:delivery_deliverysettings_change", args=[1]))

    def object_title(self, obj: models.Model) -> str:
        return gettext("Yetkazish sozlamalari")

    # --- writes through the services ------------------------------------------------------------------------

    def service_save(self, request: HttpRequest, obj: models.Model, form: Any, change: bool) -> None:
        if form is None:
            raise ValidationError(gettext("Sozlamalar faqat shu sahifadan saqlanadi."))
        changes = {
            name: form.cleaned_data[name] for name in form.changed_data if name in services.SETTINGS_FIELDS
        }
        if changes:
            services.update_settings(changes, actor=request.user, request=request)

    def service_delete(self, request: HttpRequest, obj: models.Model) -> None:
        raise ValidationError(gettext("Yetkazish sozlamalarini o‘chirib bo‘lmaydi."))

    # --- the calendar and what Yandex offers at the warehouse ----------------------------------------------

    @display(description=_("Ish vaqti va maxsus kunlar"))
    def calendar(self, obj: DeliverySettings) -> SafeString:
        """The week and the coming special days (read here; edited on their own pages, linked)."""
        hours = {row.weekday: row for row in WarehouseHours.objects.all()}
        closed = gettext("yopiq")
        week = [
            (
                gettext(name),
                f"{hours[index].opens_at:%H:%M}–{hours[index].closes_at:%H:%M}" if index in hours else closed,
            )
            for index, name in enumerate(labels.WEEKDAYS)
        ]
        today = timeutils.local_date(timeutils.now())
        days = [
            (
                row.date.isoformat(),
                closed
                if row.opens_at is None or row.closes_at is None
                else f"{row.opens_at:%H:%M}–{row.closes_at:%H:%M}",
            )
            for row in WarehouseClosure.objects.filter(date__gte=today).order_by("date")[:CALENDAR_DAYS_SHOWN]
        ]
        links = [
            (reverse("admin:delivery_warehousehours_changelist"), gettext("Ish vaqtini tahrirlash")),
            (reverse("admin:delivery_warehouseclosure_changelist"), gettext("Maxsus kunlarni tahrirlash")),
        ]
        rendered: SafeString = format_html(
            "{}{}<div>{}</div>",
            format_html_join("", "<div>{}: {}</div>", week),
            format_html_join("", "<div>{}: {}</div>", days),
            format_html_join(" · ", '<a href="{}" class="gd-cell-link">{}</a>', links),
        )
        return rendered

    @display(description=_("Yandex klasslari (oxirgi tekshiruv)"))
    def yandex_tariffs(self, obj: DeliverySettings) -> SafeString | str:
        data = integration_selectors.provider_data("yandex")
        tariffs = data.get("tariffs")
        if not isinstance(tariffs, list):
            return gettext("Hali tekshirilmagan")
        rows = [
            (gettext("Klasslar"), ", ".join(str(name) for name in tariffs) or "-"),
            (gettext("Rejim"), str(data.get("tariffs_mode") or "-")),
            (gettext("Tekshirilgan"), str(data.get("tariffs_checked_at") or "-")),
        ]
        rendered: SafeString = format_html_join("", "<div>{}: {}</div>", rows)
        return rendered

    def yandex_check(self, request: HttpRequest, object_id: str | None = None, **kwargs: Any) -> HttpResponse:
        """«Yandex bilan tekshirish»: the confirm step is the standard action page; the confirmed POST asks
        Yandex here, **outside** a transaction (``ops_action`` would wrap the call in one), and reports the
        classes found or the error code."""
        if request.method == "POST":
            if not is_active_staff(request):
                raise PermissionDenied
            obj = self.get_object(request, unquote(object_id)) if object_id is not None else None
            form = ConfirmForm(request.POST, request=request, obj=obj)
            if form.is_valid():
                result = services.check_yandex_tariffs(actor=request.user, request=request)
                messages.add_message(request, *tariff_check_message(result))
                return HttpResponseRedirect(reverse("admin:delivery_deliverysettings_change", args=[1]))
        response: HttpResponse = _yandex_check_confirm(self, request, object_id, **kwargs)
        return response


vars(DeliverySettingsAdmin.yandex_check).update(
    {name: vars(_yandex_check_confirm)[name] for name in _ACTION_ATTRIBUTES}
)
vars(DeliverySettingsAdmin.yandex_check)["original_function_name"] = "yandex_check"

admin.site.register(DeliverySettings, DeliverySettingsAdmin)
