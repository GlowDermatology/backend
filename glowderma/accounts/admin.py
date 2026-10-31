"""Read-only Django admin for the accounts models.

``Customer`` is registered by ``glowderma.customers.admin``: its change page also shows the customer's
orders, loyalty account and appointments from the upper modules.

Secrets never reach the page: ``StaffUser.password``, ``Customer.password``, ``AuthSession.refresh_hash`` /
``previous_refresh_hash``, ``Device.fcm_token`` and ``SmsCode.code_hmac`` / ``verification_token_hash`` are
excluded (design 01 §11), and so is the TOTP seed of the ops console's 2FA devices (``TOTPDevice.key``);
recovery codes (``StaticToken``, keyed hashes) are never listed, only how many are left.
List pages show phones and emails masked; the change page of a record shows them in full.

``auth.Group`` is unregistered: staff have one role model (GST-1), groups are never used.

The one write on these pages is the manager-only, confirmed and audited "2FA ni qayta o‘rnatish" action on
the staff list (``glowderma.accounts.console.access.reset_two_factor``).
"""

from typing import Any

from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Group
from django.db.models import QuerySet
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

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
from glowderma.core.admin import (
    customer_column,
    date_range,
    dropdown,
    masked_email_column,
    masked_phone_column,
    register_read_only,
)

# registered by django.contrib.auth / django_otp (editable, stock; otp_static's inline lists the tokens)
for _model in (Group, TOTPDevice, StaticDevice):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)

RESET_2FA_REASON = "ops console: 2FA ni qayta o‘rnatish"


def _is_manager(request: HttpRequest) -> bool:
    user = request.user
    return bool(user.is_active and getattr(user, "is_manager", False))


@admin.action(description="2FA ni qayta o‘rnatish", permissions=["manager"])
def reset_two_factor(
    modeladmin: admin.ModelAdmin[StaffUser], request: HttpRequest, queryset: QuerySet[StaffUser]
) -> TemplateResponse | None:
    """Delete the selected staff members' TOTP devices and recovery codes (they enrol again at the next
    login). Managers only, after a confirmation page; never one's own (use ``manage.py ops_access``)."""
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
        }
        request.current_app = modeladmin.admin_site.name
        return TemplateResponse(request, "accounts/console/reset_2fa_confirmation.html", context)
    actor: Any = request.user
    for user in targets:
        access.reset_two_factor(user, actor=actor, reason=RESET_2FA_REASON, request=request)
    modeladmin.message_user(
        request,
        f"{len(targets)} ta xodimning 2FA qurilmasi o‘chirildi: keyingi kirishda qayta sozlashadi.",
        level=messages.SUCCESS,
    )
    return None


def _has_manager_permission(self: admin.ModelAdmin[StaffUser], request: HttpRequest) -> bool:
    return _is_manager(request)


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
    actions=(reset_two_factor,),
    has_manager_permission=_has_manager_permission,
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


def _recovery_codes_left(device: StaticDevice) -> int:
    return int(device.token_set.count())


_recovery_codes_left.short_description = "Qolgan kodlar"  # type: ignore[attr-defined]

register_read_only(
    StaticDevice,
    list_display=("id", "user", "name", "confirmed", "created_at", "last_used_at", _recovery_codes_left),
    list_filter=("confirmed",),
    search_fields=("user__email", "name"),
    ordering=("-id",),
    list_select_related=("user",),
)
