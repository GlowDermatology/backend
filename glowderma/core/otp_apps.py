"""Uzbek app names for django_otp's device apps in the ops console's index and breadcrumbs.

django_otp's own ``DefaultConfig`` classes carry no ``verbose_name`` (the index would show "Otp_Totp"), so
``INSTALLED_APPS`` lists these subclasses instead. The app label (and so the migrations and tables) stays
``otp_totp`` / ``otp_static``; only the displayed name changes.
"""

from django_otp.plugins.otp_static.apps import DefaultConfig as StaticDefaultConfig
from django_otp.plugins.otp_totp.apps import DefaultConfig as TotpDefaultConfig


class OtpTotpConfig(TotpDefaultConfig):  # type: ignore[misc]
    verbose_name = "Ikki bosqichli kirish (TOTP)"


class OtpStaticConfig(StaticDefaultConfig):  # type: ignore[misc]
    verbose_name = "Tiklash kodlari"
