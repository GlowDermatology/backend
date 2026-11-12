"""Names for django_otp's device apps in the ops console's index, breadcrumbs and device pages.

django_otp's own ``DefaultConfig`` classes carry no ``verbose_name`` (the index would show "Otp_Totp"), so
``INSTALLED_APPS`` lists these subclasses instead. The app label (and so the migrations and tables) stays
``otp_totp`` / ``otp_static``; only the displayed name changes.

The device models' names and their fields' labels are plain English strings in django_otp ("TOTP device",
"static device" derived from the class name, "Last used at", …) with no translation hook. ``ready()`` gives
them the console's own in one place (:func:`uzbek_labels`), display only — written in Uzbek and lazy, like
every console text (``glowderma.core.console_i18n``), so they follow the viewer's language:

* the model names on ``Options.verbose_name`` / ``verbose_name_plural`` — ``Options.original_attrs`` (what the
  migration autodetector reads) keeps django_otp's values;
* the fields on ``Field.verbose_name`` — migrations read ``Field._verbose_name`` (what the field was declared
  with).

So django_otp's migration state stays exactly its own and no migration of the third-party apps is ever needed.
Their English help texts are hidden on the console pages instead (``ReadOnlyAdmin.field_help_texts``,
glowderma.accounts.admin).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.utils.translation import gettext_lazy as _
from django_otp.plugins.otp_static.apps import DefaultConfig as StaticDefaultConfig
from django_otp.plugins.otp_totp.apps import DefaultConfig as TotpDefaultConfig

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise

DEVICE_FIELD_LABELS: dict[str, _StrPromise] = {
    "user": _("xodim"),
    "name": _("nomi"),
    "confirmed": _("tasdiqlangan"),
    "throttling_failure_timestamp": _("oxirgi xato urinish"),
    "throttling_failure_count": _("ketma-ket xato urinishlar"),
    "created_at": _("yaratilgan"),
    "last_used_at": _("oxirgi ishlatilgan"),
}
TOTP_FIELD_LABELS: dict[str, _StrPromise] = {
    **DEVICE_FIELD_LABELS,
    "key": _("maxfiy kalit"),
    "step": _("qadam (soniya)"),
    "t0": _("boshlang‘ich vaqt (t0)"),
    "digits": _("kod uzunligi"),
    "tolerance": _("ruxsat etilgan qadamlar"),
    "drift": _("soat og‘ishi (qadam)"),
    "last_t": _("oxirgi tasdiqlangan qadam"),
}


def label_fields(model: Any, labels: dict[str, _StrPromise]) -> None:
    """Show ``model``'s fields with the Uzbek ``labels`` (display only; see the module doc)."""
    for field in model._meta.get_fields():
        if field.name in labels and hasattr(field, "verbose_name"):
            field.verbose_name = labels[field.name]


def uzbek_labels(model: Any, names: tuple[_StrPromise, _StrPromise], fields: dict[str, _StrPromise]) -> None:
    """The model's Uzbek singular/plural names and field labels (display only; see the module doc)."""
    model._meta.verbose_name, model._meta.verbose_name_plural = names
    label_fields(model, fields)


class OtpTotpConfig(TotpDefaultConfig):  # type: ignore[misc]
    verbose_name = _("Ikki bosqichli kirish (TOTP)")
    device_names = (_("2FA qurilma"), _("2FA qurilmalar"))

    def ready(self) -> None:
        super().ready()
        uzbek_labels(self.get_model("TOTPDevice"), self.device_names, TOTP_FIELD_LABELS)


class OtpStaticConfig(StaticDefaultConfig):  # type: ignore[misc]
    verbose_name = _("Tiklash kodlari")
    device_names = (_("tiklash kodlari to‘plami"), _("tiklash kodlari to‘plamlari"))

    def ready(self) -> None:
        super().ready()
        uzbek_labels(self.get_model("StaticDevice"), self.device_names, DEVICE_FIELD_LABELS)
