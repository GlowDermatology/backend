"""Uzbek app and model names for django_otp's device apps in the ops console.

django_otp's own ``DefaultConfig`` classes carry no ``verbose_name`` (the index would show "Otp_Totp"), so
``INSTALLED_APPS`` lists these subclasses instead. The app label (and so the migrations and tables) stays
``otp_totp`` / ``otp_static``; only the displayed name changes.

The device models' names are plain English strings in django_otp ("TOTP device", and "static device"
derived from the class name), shown as the list title, the change page heading and the browser title. They
are replaced on the model options at start-up: ``Options.original_attrs`` (what the migration autodetector
reads) keeps django_otp's values, so no migration of the third-party apps is ever needed.
"""

from django.db.models import Model
from django_otp.plugins.otp_static.apps import DefaultConfig as StaticDefaultConfig
from django_otp.plugins.otp_totp.apps import DefaultConfig as TotpDefaultConfig


class OtpTotpConfig(TotpDefaultConfig):  # type: ignore[misc]
    verbose_name = "Ikki bosqichli kirish (TOTP)"
    device_names = ("2FA qurilma", "2FA qurilmalar")

    def ready(self) -> None:
        super().ready()
        _rename(self.get_model("TOTPDevice"), *self.device_names)


class OtpStaticConfig(StaticDefaultConfig):  # type: ignore[misc]
    verbose_name = "Tiklash kodlari"
    device_names = ("tiklash kodlari to‘plami", "tiklash kodlari to‘plamlari")

    def ready(self) -> None:
        super().ready()
        _rename(self.get_model("StaticDevice"), *self.device_names)


def _rename(model: type[Model], singular: str, plural: str) -> None:
    model._meta.verbose_name = singular
    model._meta.verbose_name_plural = plural
