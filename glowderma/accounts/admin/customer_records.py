"""Read-only ops console pages of the customer records accounts owns: devices, addresses, one-time codes and
linked social identities (``Customer`` itself is registered by ``glowderma.customers.admin``)."""

from __future__ import annotations

from glowderma.accounts.models import (
    Address,
    CustomerSocialIdentity,
    Device,
    SmsCode,
)
from glowderma.core.admin import (
    customer_column,
    date_range,
    dropdown,
    masked_email_column,
    masked_phone_column,
    register_read_only,
)

register_read_only(
    Device,
    exclude=("fcm_token", "key_hash"),  # key_hash: SHA-256 of the device key, like a refresh hash
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
    exclude=("provider_refresh_token",),  # Apple's sealed refresh token (core.secretbox)
    list_display=("id", customer_column(), "provider", masked_email_column("email_at_link"), "created_at"),
    list_filter=("provider",),
    search_fields=("subject", "email_at_link"),
    ordering=("-id",),
    list_select_related=("customer",),
    autocomplete_fields=("customer",),
)
