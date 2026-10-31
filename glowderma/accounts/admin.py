"""Read-only Django admin for the accounts models.

``Customer`` is registered by ``glowderma.customers.admin``: its change page also shows the customer's
orders, loyalty account and appointments from the upper modules.

Secrets never reach the page: ``StaffUser.password``, ``Customer.password``, ``AuthSession.refresh_hash`` /
``previous_refresh_hash``, ``Device.fcm_token`` and ``SmsCode.code_hmac`` / ``verification_token_hash`` are
excluded (design 01 §11), and so is the TOTP seed of the ops console's 2FA devices (``TOTPDevice.key``).
List pages show phones and emails masked; the change page of a record shows them in full.

``auth.Group`` is unregistered: staff have one role model (GST-1), groups are never used.

Safe audited action (glowderma.core.admin_actions): «Seansni tugatish» signs one open customer session out
through ``accounts.sessions.revoke_session`` — the same service as ``DELETE admin/customers/{id}/sessions/
{sid}/`` (reason ``admin_revoked``) — from the session's change page or its row on the list.
"""

from typing import Any

from django.contrib import admin
from django.contrib.auth.models import Group
from django.http import HttpRequest
from django.urls import reverse
from django.utils.safestring import SafeString
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts import sessions
from glowderma.accounts.models import (
    Address,
    AuthSession,
    CustomerSocialIdentity,
    Device,
    SmsCode,
    SocialIdentity,
    StaffUser,
)
from glowderma.core import timeutils
from glowderma.core.admin import (
    ReadOnlyAdmin,
    customer_column,
    date_range,
    display,
    dropdown,
    masked_email_column,
    masked_phone_column,
    register_read_only,
    row_action_link,
)
from glowderma.core.admin_actions import ActionResult, ConfirmForm, OpsActionsMixin, done, ops_action, refused

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


def _session_refusal(model_admin: Any, request: HttpRequest | None, session: AuthSession) -> str:
    if session.audience != "customer" or session.customer_id is None:
        return "Xodim seanslari bu yerda tugatilmaydi."
    if session.revoked_at is not None or session.absolute_expires_at <= timeutils.now():
        return "Seans allaqachon tugagan."
    return ""


class AuthSessionAdmin(OpsActionsMixin, ReadOnlyAdmin):
    exclude = ("refresh_hash", "previous_refresh_hash")
    list_display = (
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
        "revoke_button",
    )
    list_filter = (dropdown("audience"), dropdown("revoke_reason"), date_range("created_at"))
    search_fields = ("id", "staff__email", "customer__phone", "device_label")
    ordering = ("-created_at",)
    list_select_related = ("staff", "customer")
    autocomplete_fields = ("staff", "customer", "device")
    actions_detail = ("revoke",)

    @display(description="Amal")
    def revoke_button(self, session: AuthSession) -> SafeString | str:
        if _session_refusal(self, None, session):
            return ""
        url = reverse("admin:accounts_authsession_revoke", args=[session.pk])
        back = reverse("admin:accounts_authsession_changelist")
        return row_action_link(f"{url}?next={back}", "Tugatish", "logout")

    @ops_action(
        description="Seansni tugatish",
        title="Mijoz seansini tugatish",
        body=(
            "Mijoz shu qurilmada tizimdan chiqariladi (sabab: admin_revoked). Qurilmada boshqa ochiq seans "
            "qolmasa, unga push xabarlar yuborilmaydi. Mijoz qaytadan kirishi mumkin."
        ),
        submit="Seansni tugatish",
        icon="logout",
        available=_session_refusal,
    )
    def revoke(self, request: HttpRequest, session: AuthSession, form: ConfirmForm) -> ActionResult:
        customer_id = int(session.customer_id or 0)
        try:
            sessions.revoke_session(
                customer_id, str(session.pk), actor=request.user, actor_type="staff", request=request
            )
        except sessions.SessionNotFound:
            return refused("Seans allaqachon tugagan: hech narsa o‘zgarmadi.")
        return done(
            "Seans tugatildi.",
            audit_action="ops.customer_session_revoked",
            entity_type="customer",
            entity_id=customer_id,
            after={"sid": str(session.pk)},
        )


admin.site.register(AuthSession, AuthSessionAdmin)
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
