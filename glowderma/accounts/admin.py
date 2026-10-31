"""Read-only Django admin for the accounts models.

``Customer`` is registered by ``glowderma.customers.admin``: its change page also shows the customer's
orders, loyalty account and appointments from the upper modules.

Secrets never reach the page: ``StaffUser.password``, ``Customer.password``, ``AuthSession.refresh_hash`` /
``previous_refresh_hash``, ``Device.fcm_token`` and ``SmsCode.code_hmac`` / ``verification_token_hash`` are
excluded (design 01 §11), and so is the TOTP seed of the ops console's 2FA devices (``TOTPDevice.key``).
List pages show phones and emails masked; the change page of a record shows them in full.

``auth.Group`` is unregistered: staff have one role model (GST-1), groups are never used.
"""

from django.contrib import admin
from django.contrib.auth.models import Group
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.models import (
    Address,
    AuthSession,
    CustomerSocialIdentity,
    Device,
    SmsCode,
    SocialIdentity,
    StaffUser,
)
from glowderma.core.admin import (
    customer_column,
    date_range,
    dropdown,
    masked_email_column,
    masked_phone_column,
    register_read_only,
)

for _model in (Group, TOTPDevice):  # registered by django.contrib.auth / django_otp (editable, stock)
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)

register_read_only(
    StaffUser,
    exclude=("password",),
    list_display=(
        "id",
        "email",
        "full_name",
        "role",
        "is_active",
        "is_manager",
        "is_staff",
        "is_superuser",
        "last_login",
    ),
    list_filter=("is_active", "is_manager", "is_staff", "is_superuser", "role"),
    search_fields=("email", "full_name"),
    ordering=("id",),
)
register_read_only(
    AuthSession,
    exclude=("refresh_hash", "previous_refresh_hash"),
    list_display=(
        "id",
        "audience",
        "staff",
        customer_column(),
        "device_label",
        "created_at",
        "last_used_at",
        "absolute_expires_at",
        "revoked_at",
        "revoke_reason",
    ),
    list_filter=(dropdown("audience"), dropdown("revoke_reason"), date_range("created_at")),
    search_fields=("id", "staff__email", "customer__phone", "device_label"),
    ordering=("-created_at",),
    list_select_related=("staff", "customer"),
    autocomplete_fields=("staff", "customer", "device"),
)
register_read_only(
    SocialIdentity,
    list_display=("id", "staff", "provider", "subject", "email_at_link", "created_at", "last_login_at"),
    list_filter=("provider",),
    search_fields=("staff__email", "subject", "email_at_link"),
    ordering=("-id",),
    autocomplete_fields=("staff",),
)
register_read_only(
    Device,
    exclude=("fcm_token",),
    list_display=(
        "id",
        customer_column(),
        "platform",
        "model",
        "app_version",
        "push_enabled",
        "is_active",
        "last_active_at",
    ),
    list_filter=("platform", "push_enabled", "is_active"),
    search_fields=("customer__phone", "fid"),
    ordering=("-last_active_at",),
    list_select_related=("customer",),
    autocomplete_fields=("customer",),
)
register_read_only(
    Address,
    list_display=("id", customer_column(), "label", "text", "is_default"),
    list_filter=("is_default",),
    search_fields=("customer__phone", "text"),
    ordering=("-id",),
    list_select_related=("customer",),
    autocomplete_fields=("customer",),
)
register_read_only(
    SmsCode,
    exclude=("code_hmac", "verification_token_hash"),
    list_display=(
        "id",
        masked_phone_column("phone"),
        masked_email_column("email"),
        "purpose",
        "created_at",
        "expires_at",
        "consumed_at",
        "exhausted_at",
        "attempts",
    ),
    list_filter=(dropdown("purpose"), date_range("created_at")),
    search_fields=("phone", "email"),
    ordering=("-id",),
)
register_read_only(
    CustomerSocialIdentity,
    list_display=("id", customer_column(), "provider", masked_email_column("email_at_link"), "created_at"),
    list_filter=("provider",),
    search_fields=("subject", "email_at_link"),
    ordering=("-id",),
    list_select_related=("customer",),
    autocomplete_fields=("customer",),
)
register_read_only(
    TOTPDevice,
    exclude=("key",),
    list_display=(
        "id",
        "user",
        "name",
        "confirmed",
        "created_at",
        "last_used_at",
        "throttling_failure_count",
    ),
    list_filter=("confirmed",),
    search_fields=("user__email", "name"),
    ordering=("-id",),
    list_select_related=("user",),
)
