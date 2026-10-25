"""Read-only Django admin for staff users, sessions and social identities.

Secrets never reach the page: ``StaffUser.password`` and ``AuthSession.refresh_hash`` are excluded.
"""

from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.core.admin import register_read_only

register_read_only(
    StaffUser,
    exclude=("password",),
    list_display=("id", "email", "full_name", "role", "is_active", "is_staff", "is_superuser", "last_login"),
    list_filter=("is_active", "is_staff", "is_superuser", "role"),
    search_fields=("email", "full_name"),
    ordering=("id",),
)
register_read_only(
    AuthSession,
    exclude=("refresh_hash",),
    list_display=(
        "id",
        "audience",
        "staff",
        "customer_id",
        "device_label",
        "created_at",
        "last_used_at",
        "absolute_expires_at",
        "revoked_at",
        "revoke_reason",
    ),
    list_filter=("audience", "revoke_reason"),
    search_fields=("id", "staff__email", "device_label"),
    ordering=("-created_at",),
    raw_id_fields=("staff",),
)
register_read_only(
    SocialIdentity,
    list_display=("id", "staff", "provider", "subject", "email_at_link", "created_at", "last_login_at"),
    list_filter=("provider",),
    search_fields=("staff__email", "subject", "email_at_link"),
    ordering=("-id",),
    raw_id_fields=("staff",),
)
