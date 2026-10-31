"""The customer page of the ops console: one read-only record with everything that hangs off it.

``Customer`` lives in ``accounts``, but its change page shows rows of the upper modules (orders, the loyalty
account, appointments) as inline tabs, so it is registered here, in the top layer. Secrets stay out:
``Customer.password`` is excluded, and the device/session inlines list no token or hash column.
"""

from django.contrib import admin

from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.appointments.models import Appointment
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
    status_column,
)
from glowderma.core.labels import CUSTOMER_STATUS
from glowderma.loyalty.models import LoyaltyAccount


class CustomerOrderInline(ReadOnlyTabularInline):
    model = Order
    fk_name = "customer"
    fields = ("id", "status", "fulfilment", "total", "payment_provider", "channel", "created_at")
    ordering = ("-created_at", "-id")
    per_page = 10
    tab = True
    verbose_name_plural = "Buyurtmalar"


class CustomerLoyaltyInline(ReadOnlyTabularInline):
    model = LoyaltyAccount
    fields = ("balance", "updated_at")
    tab = True
    verbose_name_plural = "Cashback"


class CustomerAppointmentInline(ReadOnlyTabularInline):
    model = Appointment
    fields = ("start", "professional", "service", "status", "requested_via", "price")
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


class CustomerAdmin(ReadOnlyAdmin):
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


admin.site.register(Customer, CustomerAdmin)
