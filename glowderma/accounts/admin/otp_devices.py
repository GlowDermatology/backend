"""The ops console's second-factor devices: django_otp's editable admins are replaced by read-only ones that
never show the TOTP seed or a recovery code. Unregistering and re-registering stay in this one module, so no
other module's import order can drop the read-only admins."""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.core.admin import (
    register_read_only,
)
from glowderma.core.otp_apps import TOTP_FIELD_LABELS

# registered by django_otp (editable, stock; otp_static's inline lists the tokens)
for _model in (TOTPDevice, StaticDevice):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)

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


_recovery_codes_left.short_description = _("Qolgan kodlar")  # type: ignore[attr-defined]

register_read_only(
    StaticDevice,
    field_help_texts=OTP_HELP_TEXTS_HIDDEN,
    list_display=("id", "user", "name", "confirmed", "created_at", "last_used_at", _recovery_codes_left),
    list_filter=("confirmed",),
    search_fields=("user__email", "name"),
    ordering=("-id",),
    list_select_related=("user",),
)
