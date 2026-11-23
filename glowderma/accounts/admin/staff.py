"""The staff pages of the ops console: the read-only staff list with the manager-only "2FA ni qayta
o‘rnatish" action, and the staff members' linked social identities. ``auth.Group`` is unregistered (GST-1)."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Group
from django.db.models import QuerySet
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _

from glowderma.accounts.console import access
from glowderma.accounts.models import (
    SocialIdentity,
    StaffUser,
)
from glowderma.core.admin import (
    register_read_only,
)

# registered by django.contrib.auth (stock, editable): staff have one role model (GST-1)
if admin.site.is_registered(Group):
    admin.site.unregister(Group)

RESET_2FA_REASON = "ops console: 2FA ni qayta o‘rnatish"  # the audit row's reason: stored, never translated


def _is_manager(request: HttpRequest) -> bool:
    user = request.user
    return bool(user.is_active and getattr(user, "is_manager", False))


def _enrol_window_text() -> str:
    """``OPS_ENROL_WINDOW_SECONDS`` in the console's language (Uzbek: ``24 soat``, ``90 daqiqa``)."""
    seconds = int(settings.OPS_ENROL_WINDOW_SECONDS)
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return ngettext("%(count)s soat", "%(count)s soat", hours) % {"count": hours}
    minutes = seconds // 60
    return ngettext("%(count)s daqiqa", "%(count)s daqiqa", minutes) % {"count": minutes}


@admin.action(description=_("2FA ni qayta o‘rnatish"), permissions=["manager"])
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
            request, gettext("O‘z 2FA ingizni bu yerdan qayta o‘rnatib bo‘lmaydi."), level=messages.ERROR
        )
        return None
    if request.POST.get("post") != "yes":
        context = {
            **modeladmin.admin_site.each_context(request),
            "title": gettext("2FA ni qayta o‘rnatish"),
            "opts": modeladmin.model._meta,
            "targets": targets,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "enrol_window": _enrol_window_text(),
        }
        request.current_app = modeladmin.admin_site.name
        return TemplateResponse(request, "admin/accounts/staffuser/reset_2fa_confirmation.html", context)
    actor: Any = request.user
    for user in targets:
        access.reset_two_factor(
            user, actor=actor, reason=RESET_2FA_REASON, request=request, via=access.VIA_CONSOLE
        )
    modeladmin.message_user(
        request,
        ngettext(
            "%(count)s ta xodimning 2FA qurilmasi o‘chirildi va konsol sessiyalari tugatildi: ular "
            "%(window)s ichida keyingi kirishda 2FA ni qayta sozlashadi.",
            "%(count)s ta xodimning 2FA qurilmasi o‘chirildi va konsol sessiyalari tugatildi: ular "
            "%(window)s ichida keyingi kirishda 2FA ni qayta sozlashadi.",
            len(targets),
        )
        % {"count": len(targets), "window": _enrol_window_text()},
        level=messages.SUCCESS,
    )
    return None


def _has_manager_permission(self: admin.ModelAdmin[StaffUser], request: HttpRequest) -> bool:
    return _is_manager(request)


register_read_only(
    StaffUser,
    title_format=_("Xodim: {obj.email}"),
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


register_read_only(
    SocialIdentity,
    list_display=("id", "staff", "provider", "subject", "email_at_link", "created_at", "last_login_at"),
    list_filter=("provider",),
    search_fields=("staff__email", "subject", "email_at_link"),
    ordering=("-id",),
    autocomplete_fields=("staff",),
)
