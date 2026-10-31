"""Uzbek names for django_otp's device apps in the ops console's index, breadcrumbs and device pages.

django_otp's own ``DefaultConfig`` classes carry no ``verbose_name`` (the index would show "Otp_Totp"), so
``INSTALLED_APPS`` lists these subclasses instead. The app label (and so the migrations and tables) stays
``otp_totp`` / ``otp_static``; only the displayed name changes.

The device models' names and their fields' labels are plain English strings in django_otp ("TOTP device",
"static device" derived from the class name, "Last used at", …) with no translation hook. ``ready()`` gives
them Uzbek ones in one place (:func:`uzbek_labels`), display only:

* the model names on ``Options.verbose_name`` / ``verbose_name_plural`` — ``Options.original_attrs`` (what the
  migration autodetector reads) keeps django_otp's values;
* the fields on ``Field.verbose_name`` — migrations read ``Field._verbose_name`` (what the field was declared
  with).

So django_otp's migration state stays exactly its own and no migration of the third-party apps is ever needed.
Their English help texts are hidden on the console pages instead (``ReadOnlyAdmin.field_help_texts``,
glowderma.accounts.admin).
"""

from typing import Any

from django_otp.plugins.otp_static.apps import DefaultConfig as StaticDefaultConfig
from django_otp.plugins.otp_totp.apps import DefaultConfig as TotpDefaultConfig

DEVICE_FIELD_LABELS = {
    "user": "xodim",
    "name": "nomi",
    "confirmed": "tasdiqlangan",
    "throttling_failure_timestamp": "oxirgi xato urinish",
    "throttling_failure_count": "ketma-ket xato urinishlar",
    "created_at": "yaratilgan",
    "last_used_at": "oxirgi ishlatilgan",
}
TOTP_FIELD_LABELS = {
    **DEVICE_FIELD_LABELS,
    "key": "maxfiy kalit",
    "step": "qadam (soniya)",
    "t0": "boshlang‘ich vaqt (t0)",
    "digits": "kod uzunligi",
    "tolerance": "ruxsat etilgan qadamlar",
    "drift": "soat og‘ishi (qadam)",
    "last_t": "oxirgi tasdiqlangan qadam",
}


def label_fields(model: Any, labels: dict[str, str]) -> None:
    """Show ``model``'s fields with the Uzbek ``labels`` (display only; see the module doc)."""
    for field in model._meta.get_fields():
        if field.name in labels and hasattr(field, "verbose_name"):
            field.verbose_name = labels[field.name]


def uzbek_labels(model: Any, names: tuple[str, str], fields: dict[str, str]) -> None:
    """The model's Uzbek singular/plural names and field labels (display only; see the module doc)."""
    model._meta.verbose_name, model._meta.verbose_name_plural = names
    label_fields(model, fields)


class OtpTotpConfig(TotpDefaultConfig):  # type: ignore[misc]
    verbose_name = "Ikki bosqichli kirish (TOTP)"
    device_names = ("2FA qurilma", "2FA qurilmalar")

    def ready(self) -> None:
        super().ready()
        uzbek_labels(self.get_model("TOTPDevice"), self.device_names, TOTP_FIELD_LABELS)


class OtpStaticConfig(StaticDefaultConfig):  # type: ignore[misc]
    verbose_name = "Tiklash kodlari"
    device_names = ("tiklash kodlari to‘plami", "tiklash kodlari to‘plamlari")

    def ready(self) -> None:
        super().ready()
        uzbek_labels(self.get_model("StaticDevice"), self.device_names, DEVICE_FIELD_LABELS)
