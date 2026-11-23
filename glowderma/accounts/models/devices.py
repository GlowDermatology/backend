"""Customer devices: the app installations that sign in and receive pushes."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext

from glowderma.core.models import TimeStampedModel, timestamp_default


class Device(TimeStampedModel):
    """A mobile installation of one customer; carries the FCM token used by notifications."""

    class Platform(models.TextChoices):  # internal (same literals as enums.OrderChannel)
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"
        WEB = "web", "Veb-sayt"  # a browser of the customer website (its BFF holds the device id in a cookie)

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="devices", verbose_name="mijoz"
    )
    fid = models.CharField(max_length=128, verbose_name="FID")  # Firebase installation id
    platform = models.CharField(max_length=8, choices=Platform.choices, verbose_name="platforma")
    model = models.CharField(max_length=80, blank=True, verbose_name="model")  # "iPhone 14"
    os = models.CharField(max_length=40, blank=True, verbose_name="OT")  # "iOS 18.1"
    app_version = models.CharField(max_length=20, blank=True, verbose_name="ilova versiyasi")  # "1.0.0"
    locale = models.CharField(
        max_length=8, blank=True, verbose_name="til"
    )  # device locale as reported, informational
    fcm_token = models.CharField(max_length=512, blank=True, verbose_name="FCM token")
    push_enabled = models.BooleanField(default=False, verbose_name="push yoqilgan")
    is_active = models.BooleanField(default=True, verbose_name="faol")  # false after logout/replace/delete
    last_active_at = models.DateTimeField(default=timestamp_default, verbose_name="oxirgi faollik")
    fcm_invalidated_at = models.DateTimeField(
        null=True, verbose_name="FCM token bekor qilingan"
    )  # set when FCM reports the token dead
    # M2 (customer contract §1.9): SHA-256 hex of the device key issued once on a verified sign-in; "" = none.
    key_hash = models.CharField(max_length=64, blank=True, verbose_name="kalit xeshi")

    class Meta:
        verbose_name = "qurilma"
        verbose_name_plural = "qurilmalar"
        constraints = [
            models.UniqueConstraint(fields=["customer", "fid"], name="device_customer_fid_unique"),
            models.UniqueConstraint(
                fields=["fcm_token"],
                condition=Q(is_active=True) & ~Q(fcm_token=""),
                name="device_active_fcm_token_unique",
            ),
            models.CheckConstraint(
                condition=Q(push_enabled=False) | ~Q(fcm_token=""), name="device_push_requires_token"
            ),
        ]
        indexes = [
            models.Index(
                fields=["customer", "is_active", "last_active_at"], name="accounts_device_cust_active"
            ),
            models.Index(
                fields=["push_enabled"],
                condition=Q(is_active=True, push_enabled=True),
                name="accounts_device_pushable",
            ),
        ]

    def __str__(self) -> str:
        return gettext("%(platform)s %(model)s · mijoz #%(customer)s") % {
            "platform": self.get_platform_display(),
            "model": self.model or self.fid,
            "customer": self.customer_id,
        }


__all__ = ["Device"]
