"""Read-only Django admin for the accounts models.

``Customer`` is registered by ``glowderma.customers.admin``: its change page also shows the customer's
orders, loyalty account and appointments from the upper modules.

Secrets never reach the page: ``StaffUser.password``, ``Customer.password``, ``AuthSession.refresh_hash`` /
``previous_refresh_hash``, ``Device.fcm_token`` / ``key_hash``, ``SmsCode.code_hmac`` /
``verification_token_hash`` and ``CustomerSocialIdentity.provider_refresh_token`` (Apple's sealed refresh
token) are excluded (design 01 §11), and so is the TOTP seed of the ops console's 2FA devices
(``TOTPDevice.key``); recovery codes (``StaticToken``, keyed hashes) are never listed, only how many are left.
List pages show phones and emails masked (so does the customer page, glowderma.customers.admin).

``auth.Group`` is unregistered: staff have one role model (GST-1), groups are never used.

Writes on these pages (each confirmed and audited):

- the manager-only "2FA ni qayta o‘rnatish" action on the staff list
  (``glowderma.accounts.console.access.reset_two_factor``);
- «Sessiyani tugatish» (glowderma.core.admin_actions) signs one open customer session out through
  ``accounts.sessions.revoke_session`` — the same service as ``DELETE admin/customers/{id}/sessions/{sid}/``
  (reason ``admin_revoked``) — from the session's change page or its row on the list.
"""

from typing import Any

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Group
from django.db.models import QuerySet
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.safestring import SafeString
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts import sessions
from glowderma.accounts.console import access
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
    forget_cached_counts,
    masked_email_column,
    masked_phone_column,
    register_read_only,
    row_action_link,
)
from glowderma.core.admin_actions import ActionResult, ConfirmForm, OpsActionsMixin, done, ops_action, refused
from glowderma.core.otp_apps import TOTP_FIELD_LABELS

# registered by django.contrib.auth / django_otp (editable, stock; otp_static's inline lists the tokens)
for _model in (Group, TOTPDevice, StaticDevice):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)

RESET_2FA_REASON = "ops console: 2FA ni qayta o‘rnatish"


def _is_manager(request: HttpRequest) -> bool:
    user = request.user
    return bool(user.is_active and getattr(user, "is_manager", False))


def _enrol_window_text() -> str:
    """``OPS_ENROL_WINDOW_SECONDS`` in Uzbek: ``24 soat``, ``90 daqiqa``."""
    seconds = int(settings.OPS_ENROL_WINDOW_SECONDS)
    return f"{seconds // 3600} soat" if seconds % 3600 == 0 else f"{seconds // 60} daqiqa"


@admin.action(description="2FA ni qayta o‘rnatish", permissions=["manager"])
def reset_two_factor(
    modeladmin: admin.ModelAdmin[StaffUser], request: HttpRequest, queryset: QuerySet[StaffUser]
) -> TemplateResponse | None:
    """Delete the selected staff members' TOTP devices and recovery codes and end their console sessions;
    they enrol again at the next login, inside the enrolment window this opens (``via`` console: refused
    at enrolment if the same manager also reset the person's password, ``access`` module doc). Managers
    only, after a confirmation page; never one's own (use ``manage.py ops_access``)."""
    targets = list(queryset.order_by("pk"))
    own = [user for user in targets if user.pk == request.user.pk]
    if own:
        modeladmin.message_user(
            request, "O‘z 2FA ingizni bu yerdan qayta o‘rnatib bo‘lmaydi.", level=messages.ERROR
        )
        return None
    if request.POST.get("post") != "yes":
        context = {
            **modeladmin.admin_site.each_context(request),
            "title": "2FA ni qayta o‘rnatish",
            "opts": modeladmin.model._meta,
            "targets": targets,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "enrol_window": _enrol_window_text(),
        }
        request.current_app = modeladmin.admin_site.name
        return TemplateResponse(request, "accounts/console/reset_2fa_confirmation.html", context)
    actor: Any = request.user
    for user in targets:
        access.reset_two_factor(
            user, actor=actor, reason=RESET_2FA_REASON, request=request, via=access.VIA_CONSOLE
        )
    modeladmin.message_user(
        request,
        f"{len(targets)} ta xodimning 2FA qurilmasi o‘chirildi va konsol sessiyalari tugatildi: ular "
        f"{_enrol_window_text()} ichida keyingi kirishda 2FA ni qayta sozlashadi.",
        level=messages.SUCCESS,
    )
    return None


def _has_manager_permission(self: admin.ModelAdmin[StaffUser], request: HttpRequest) -> bool:
    return _is_manager(request)


register_read_only(
    StaffUser,
    title_format="Xodim: {obj.email}",
    # groups and permissions are never used (GST-1): one staff role, managers flagged by is_manager
    exclude=("password", "groups", "user_permissions"),
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
    actions=(reset_two_factor,),
    has_manager_permission=_has_manager_permission,
)


def _session_refusal(model_admin: Any, request: HttpRequest | None, session: AuthSession) -> str:
    if session.audience != "customer" or session.customer_id is None:
        return "Xodim sessiyalari bu yerda tugatilmaydi."
    if session.revoked_at is not None or session.absolute_expires_at <= timeutils.now():
        return "Sessiya allaqachon tugagan."
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
    list_filter = (
        dropdown("audience"),
        dropdown("revoke_reason"),
        dropdown("device__platform"),
        date_range("created_at"),
    )
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
        description="Sessiyani tugatish",
        title="Mijoz sessiyasini tugatish",
        body=(
            "Mijoz shu qurilmada tizimdan chiqariladi (sabab: «Admin tugatdi»). Qurilmada boshqa ochiq "
            "sessiya qolmasa, unga push xabarlar yuborilmaydi. Mijoz qaytadan kirishi mumkin."
        ),
        submit="Sessiyani tugatish",
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
            return refused("Sessiya allaqachon tugagan: hech narsa o‘zgarmadi.")
        forget_cached_counts("dash:customers")
        return done(
            "Sessiya tugatildi.",
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
# django_otp's field help texts are English and have no translation hook: hidden (the labels are Uzbek,
# glowderma.core.otp_apps).
OTP_HELP_TEXTS_HIDDEN = {name: "" for name in TOTP_FIELD_LABELS}

register_read_only(
    TOTPDevice,
    exclude=("key",),
    field_help_texts=OTP_HELP_TEXTS_HIDDEN,
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


def _recovery_codes_left(device: StaticDevice) -> int:
    return int(device.token_set.count())


_recovery_codes_left.short_description = "Qolgan kodlar"  # type: ignore[attr-defined]

register_read_only(
    StaticDevice,
    field_help_texts=OTP_HELP_TEXTS_HIDDEN,
    list_display=("id", "user", "name", "confirmed", "created_at", "last_used_at", _recovery_codes_left),
    list_filter=("confirmed",),
    search_fields=("user__email", "name"),
    ordering=("-id",),
    list_select_related=("user",),
)
