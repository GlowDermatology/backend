"""The sign-in sessions page of the ops console, with «Sessiyani tugatish» for customer sessions."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from django.urls import reverse
from django.utils.safestring import SafeString
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from glowderma.accounts import services
from glowderma.accounts.models import (
    AuthSession,
)
from glowderma.core import timeutils
from glowderma.core.admin import (
    ReadOnlyAdmin,
    customer_column,
    date_range,
    display,
    dropdown,
    forget_cached_counts,
    row_action_link,
)
from glowderma.core.admin_actions import ActionResult, ConfirmForm, OpsActionsMixin, done, ops_action, refused


def _session_refusal(model_admin: Any, request: HttpRequest | None, session: AuthSession) -> str:
    if session.audience != "customer" or session.customer_id is None:
        return gettext("Xodim sessiyalari bu yerda tugatilmaydi.")
    if session.revoked_at is not None or session.absolute_expires_at <= timeutils.now():
        return gettext("Sessiya allaqachon tugagan.")
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

    @display(description=_("Amal"))
    def revoke_button(self, session: AuthSession) -> SafeString | str:
        if _session_refusal(self, None, session):
            return ""
        url = reverse("admin:accounts_authsession_revoke", args=[session.pk])
        back = reverse("admin:accounts_authsession_changelist")
        return row_action_link(f"{url}?next={back}", gettext("Tugatish"), "logout")

    @ops_action(
        description=_("Sessiyani tugatish"),
        title=_("Mijoz sessiyasini tugatish"),
        body=_(
            "Mijoz shu qurilmada tizimdan chiqariladi (sabab: «Admin tugatdi»). Qurilmada boshqa ochiq "
            "sessiya qolmasa, unga push xabarlar yuborilmaydi. Mijoz qaytadan kirishi mumkin."
        ),
        submit=_("Sessiyani tugatish"),
        icon="logout",
        available=_session_refusal,
    )
    def revoke(self, request: HttpRequest, session: AuthSession, form: ConfirmForm) -> ActionResult:
        customer_id = int(session.customer_id or 0)
        try:
            services.revoke_open_customer_session(
                customer_id, str(session.pk), actor=request.user, actor_type="staff", request=request
            )
        except services.SessionNotFound:
            return refused(gettext("Sessiya allaqachon tugagan: hech narsa o‘zgarmadi."))
        forget_cached_counts("dash:customers")
        return done(
            gettext("Sessiya tugatildi."),
            audit_action="ops.customer_session_revoked",
            entity_type="customer",
            entity_id=customer_id,
            after={"sid": str(session.pk)},
        )


admin.site.register(AuthSession, AuthSessionAdmin)
