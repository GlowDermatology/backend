"""Read-only Django admin for the accounts models.

Secrets never reach the page: ``StaffUser.password``, ``Customer.password``, ``AuthSession.refresh_hash`` /
``previous_refresh_hash``, ``Device.fcm_token`` and ``SmsCode.code_hmac`` / ``verification_token_hash`` are
excluded (design 01 §11).
"""

from glowderma.accounts.models import (
    Address,
    AuthSession,
    Customer,
    CustomerSocialIdentity,
    Device,
    SmsCode,
    SocialIdentity,
    StaffUser,
)
from glowderma.core.admin import register_read_only

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
        "customer",
        "device_label",
        "created_at",
        "last_used_at",
        "absolute_expires_at",
        "revoked_at",
        "revoke_reason",
    ),
    list_filter=("audience", "revoke_reason"),
    search_fields=("id", "staff__email", "customer__phone", "device_label"),
    ordering=("-created_at",),
    raw_id_fields=("staff", "customer", "device"),
)
register_read_only(
    SocialIdentity,
    list_display=("id", "staff", "provider", "subject", "email_at_link", "created_at", "last_login_at"),
    list_filter=("provider",),
    search_fields=("staff__email", "subject", "email_at_link"),
    ordering=("-id",),
    raw_id_fields=("staff",),
)
register_read_only(
    Customer,
    exclude=("password",),
    list_display=("id", "phone", "name", "status", "language", "registered_at"),
    list_filter=("status", "language"),
    search_fields=("phone", "name"),
    ordering=("-registered_at", "-id"),
)
register_read_only(
    Device,
    exclude=("fcm_token",),
    list_display=(
        "id",
        "customer",
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
    raw_id_fields=("customer",),
)
register_read_only(
    Address,
    list_display=("id", "customer", "label", "text", "is_default"),
    list_filter=("is_default",),
    search_fields=("customer__phone", "text"),
    ordering=("-id",),
    raw_id_fields=("customer",),
)
register_read_only(
    SmsCode,
    exclude=("code_hmac", "verification_token_hash"),
    list_display=(
        "id",
        "phone",
        "purpose",
        "created_at",
        "expires_at",
        "consumed_at",
        "exhausted_at",
        "attempts",
    ),
    list_filter=("purpose",),
    search_fields=("phone",),
    ordering=("-id",),
)
register_read_only(
    CustomerSocialIdentity,
    list_display=("id", "customer", "provider", "email_at_link", "created_at"),
    list_filter=("provider",),
    search_fields=("subject", "email_at_link"),
    ordering=("-id",),
    raw_id_fields=("customer",),
)
