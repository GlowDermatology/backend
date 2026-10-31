"""The customer page of the ops console: one read-only record with everything that hangs off it.

``Customer`` lives in ``accounts``, but its change page shows rows of the upper modules (orders, the loyalty
account, appointments) as inline tabs, so it is registered here, in the top layer. Secrets stay out:
``Customer.password`` is excluded, and the device/session inlines list no token or hash column.

Safe audited action (glowderma.core.admin_actions): «Barcha seanslarni tugatish» signs the customer out
everywhere through ``accounts.sessions.revoke_all_sessions`` (reason ``admin_revoked``).
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from glowderma.accounts import sessions
from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.appointments.models import Appointment
from glowderma.commerce.admin import ORDER_STATUS_VARIANTS
from glowderma.commerce.models import Order
from glowderma.core.admin import (
    DANGER,
    SUCCESS,
    ReadOnlyAdmin,
    ReadOnlyTabularInline,
    date_range,
    dropdown,
    masked_email_column,
    masked_phone_column,
    money_column,
    status_badge,
    status_column,
)
from glowderma.core.admin_actions import (
    ActionResult,
    ActionVariant,
    ConfirmForm,
    OpsActionsMixin,
    done,
    ops_action,
    refused,
)
from glowderma.core.labels import CUSTOMER_STATUS, ORDER_STATUS
from glowderma.loyalty.models import LoyaltyAccount


class CustomerOrderInline(ReadOnlyTabularInline):
    model = Order
    fk_name = "customer"
    fields = ("id", "status_label", "fulfilment", "total_som", "payment_provider", "channel", "created_at")
    ordering = ("-created_at", "-id")
    status_label = staticmethod(status_badge("status", "Holat", ORDER_STATUS_VARIANTS, ORDER_STATUS))
    total_som = staticmethod(money_column("total", "Jami"))
    per_page = 10
    tab = True
    verbose_name_plural = "Buyurtmalar"


class CustomerLoyaltyInline(ReadOnlyTabularInline):
    model = LoyaltyAccount
    fields = ("balance_som", "updated_at")
    balance_som = staticmethod(money_column("balance", "Balans"))
    tab = True
    verbose_name_plural = "Cashback"


class CustomerAppointmentInline(ReadOnlyTabularInline):
    model = Appointment
    fields = ("start", "professional", "service", "status", "requested_via", "price_som")
    price_som = staticmethod(money_column("price", "Narx"))
    ordering = ("-start", "-id")
    per_page = 10
    tab = True
    verbose_name_plural = "Qabullar"


class CustomerAddressInline(ReadOnlyTabularInline):
    model = Address
    fields = ("label", "text", "is_default")
    tab = True
    verbose_name_plural = "Manzillar"


class CustomerDeviceInline(ReadOnlyTabularInline):
    model = Device
    fields = ("platform", "model", "app_version", "push_enabled", "is_active", "last_active_at")
    ordering = ("-last_active_at",)
    tab = True
    verbose_name_plural = "Qurilmalar"


class CustomerSessionInline(ReadOnlyTabularInline):
    model = AuthSession
    fk_name = "customer"
    fields = (
        "device_label",
        "created_at",
        "last_used_at",
        "absolute_expires_at",
        "revoked_at",
        "revoke_reason",
    )
    ordering = ("-created_at",)
    per_page = 10
    tab = True
    verbose_name_plural = "Sessiyalar"


class CustomerSocialInline(ReadOnlyTabularInline):
    model = CustomerSocialIdentity
    fields = ("provider", "email_at_link", "created_at")
    tab = True
    verbose_name_plural = "Ijtimoiy kirishlar"


def _no_open_session(model_admin: Any, request: HttpRequest, customer: Customer) -> str:
    return "" if sessions.open_sessions(customer.pk).exists() else "Mijozning ochiq seansi yo‘q."


class CustomerAdmin(OpsActionsMixin, ReadOnlyAdmin):
    exclude = ("password",)
    list_display = (
        "id",
        "name",
        masked_phone_column("phone"),
        masked_email_column("email"),
        status_column("status", "Holat", {"active": SUCCESS, "deleted": DANGER}, CUSTOMER_STATUS),
        "language",
        "registered_at",
    )
    list_filter = (dropdown("status"), dropdown("language"), date_range("registered_at"))
    search_fields = ("phone", "email", "name")
    ordering = ("-registered_at", "-id")
    date_hierarchy = "registered_at"
    inlines = (
        CustomerOrderInline,
        CustomerLoyaltyInline,
        CustomerAppointmentInline,
        CustomerAddressInline,
        CustomerDeviceInline,
        CustomerSessionInline,
        CustomerSocialInline,
    )
    tab_groups = (
        (
            "Mijoz",
            (
                "id",
                "name",
                "phone",
                "email",
                "email_verified_at",
                "status",
                "language",
                "image",
                "address_text",
            ),
        ),
        (
            "Kirish",
            (
                "registered_at",
                "last_login_at",
                "password_changed_at",
                "deleted_at",
                "reactivated_at",
                "history_from",
            ),
        ),
        ("Rozilik", ("terms_version", "consent_at", "consent_ip")),
    )
    actions_detail = ("revoke_all_sessions",)

    @ops_action(
        description="Barcha seanslarni tugatish",
        title="Mijozning barcha seanslarini tugatish",
        body=(
            "Mijoz barcha telefon va brauzerlardan chiqariladi (sabab: admin_revoked), seanssiz qolgan "
            "qurilmalarga push yuborilmaydi. Yo‘qolgan telefon yoki begona kirish gumon qilinganda ishlating."
        ),
        submit="Hammasini tugatish",
        icon="logout",
        variant=ActionVariant.DANGER,
        url_path="revoke-all-sessions",
        available=_no_open_session,
    )
    def revoke_all_sessions(
        self, request: HttpRequest, customer: Customer, form: ConfirmForm
    ) -> ActionResult:
        count = sessions.revoke_all_sessions(customer.pk, actor=request.user, request=request)
        if not count:
            return refused("Mijozning ochiq seansi yo‘q: hech narsa o‘zgarmadi.")
        return done(
            f"{count} ta seans tugatildi.",
            audit_action="ops.customer_sessions_revoked",
            entity_type="customer",
            entity_id=customer.pk,
            after={"count": count},
        )


admin.site.register(Customer, CustomerAdmin)
