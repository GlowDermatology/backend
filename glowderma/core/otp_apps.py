"""Uzbek names for django_otp's device apps in the ops console's index, breadcrumbs and device pages.

django_otp's own ``DefaultConfig`` classes carry no ``verbose_name`` (the index would show "Otp_Totp"), so
``INSTALLED_APPS`` lists these subclasses instead. The app label (and so the migrations and tables) stays
``otp_totp`` / ``otp_static``; only the displayed name changes.

Their model fields have English labels with no translation hook (plain strings, not ``gettext``): ``ready()``
gives them Uzbek ones. Only ``Field.verbose_name`` changes — migrations read ``Field._verbose_name`` (what
the field was declared with), so django_otp's migration state stays exactly its own. Their English help texts
are hidden on the console pages instead (``ReadOnlyAdmin.field_help_texts``, glowderma.accounts.admin).
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


class OtpTotpConfig(TotpDefaultConfig):  # type: ignore[misc]
    verbose_name = "Ikki bosqichli kirish (TOTP)"

    def ready(self) -> None:
        super().ready()
        label_fields(self.get_model("TOTPDevice"), TOTP_FIELD_LABELS)


class OtpStaticConfig(StaticDefaultConfig):  # type: ignore[misc]
    verbose_name = "Tiklash kodlari"

    def ready(self) -> None:
        super().ready()
        label_fields(self.get_model("StaticDevice"), DEVICE_FIELD_LABELS)
