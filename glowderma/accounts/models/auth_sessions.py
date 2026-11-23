"""Sign-in sessions of both audiences (staff and customer) with their refresh-token state."""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext

from glowderma.accounts.models.defaults import date_joined_default


class AuthSession(models.Model):
    class AuthMethod(models.TextChoices):  # internal: how the session's sign-in was proven
        PASSWORD = "password", "Parol"  # noqa: S105 - staff email + password, customer password (or SMS-verified)
        GOOGLE = "google", "Google"
        FACEBOOK = "facebook", "Facebook"
        APPLE = "apple", "Apple"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audience = models.CharField(
        max_length=16, choices=[("staff", "Xodim"), ("customer", "Mijoz")], verbose_name="auditoriya"
    )
    staff = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="sessions",
        verbose_name="xodim",
    )
    customer = models.ForeignKey(
        "accounts.Customer",
        null=True,
        on_delete=models.PROTECT,
        related_name="sessions",
        verbose_name="mijoz",
    )
    # M2
    device = models.ForeignKey(
        "accounts.Device",
        null=True,
        on_delete=models.SET_NULL,
        related_name="sessions",
        verbose_name="qurilma",
    )
    device_label = models.CharField(max_length=100, verbose_name="qurilma nomi")
    refresh_hash = models.CharField(max_length=64, unique=True, verbose_name="refresh token xeshi")
    created_at = models.DateTimeField(default=date_joined_default, editable=False, verbose_name="yaratilgan")
    last_used_at = models.DateTimeField(default=date_joined_default, verbose_name="oxirgi foydalanish")
    absolute_expires_at = models.DateTimeField(verbose_name="mutlaq muddat")
    revoked_at = models.DateTimeField(null=True, verbose_name="tugatilgan")
    revoke_reason = models.CharField(
        max_length=24,
        blank=True,
        choices=[
            ("logout", "Chiqish"),
            ("expired", "Muddati tugadi"),
            ("password_changed", "Parol o‘zgartirildi"),
            ("deactivated", "Hisob faolsizlantirildi"),
            ("replaced", "Yangi sessiya bilan almashtirildi"),
            ("admin_revoked", "Admin tugatdi"),
            ("refresh_reused", "Eski token qayta ishlatildi"),
        ],
        verbose_name="tugatish sababi",
    )
    # M2 rotating refresh (customers only; staff refresh is non-rotating and leaves these empty)
    previous_refresh_hash = models.CharField(
        max_length=64, blank=True, verbose_name="oldingi refresh token xeshi"
    )
    rotated_at = models.DateTimeField(null=True, verbose_name="token yangilangan")
    # Customer contract §2.1.6: the access-token claim ``rot`` is this counter (never a timestamp); the first
    # request carrying the current ``rot`` sets ``rotation_confirmed`` (CustomerJWTAuthentication).
    rotation = models.PositiveIntegerField(default=0, verbose_name="token yangilanishlari soni")
    rotation_confirmed = models.BooleanField(default=False, verbose_name="yangilanish tasdiqlangan")
    # How the sign-in that opened this session was proven (admin contract §2.1: linking a staff identity
    # needs a password session). Kept on the row, so a customer refresh rotation carries it; "" = before 0007.
    auth_method = models.CharField(
        max_length=16, choices=AuthMethod.choices, blank=True, verbose_name="kirish usuli"
    )
    # B-W1 session management (customer contract §2.2.10): the client's user agent (the website's BFF
    # forwards the browser's as X-GD-Client-UA), the last client IP and the place a local GeoIP City database
    # resolves it to ("" = unknown / no database). Customer sessions only; refreshed by the throttled activity
    # bump (CustomerJWTAuthentication).
    user_agent = models.CharField(
        max_length=512, blank=True, default="", db_default="", verbose_name="user-agent"
    )
    last_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="oxirgi IP")
    location = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="joylashuv"
    )
    # Admin panel TOTP (feat/admin-ux): when this staff sign-in's second factor was verified; null = none.
    mfa_verified_at = models.DateTimeField(null=True, blank=True, verbose_name="MFA tasdiqlangan")

    class Meta:
        verbose_name = "sessiya"
        verbose_name_plural = "sessiyalar"
        indexes = [
            models.Index(fields=["customer", "revoked_at"], name="accounts_authsession_cust_rev"),
            models.Index(fields=["staff", "revoked_at"], name="accounts_authsession_staff_rev"),
            models.Index(
                fields=["previous_refresh_hash"],
                condition=~Q(previous_refresh_hash=""),
                name="authsession_prev_refresh_idx",  # Django caps index names at 30
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(audience="staff", staff__isnull=False, customer__isnull=True)
                    | Q(audience="customer", customer__isnull=False, staff__isnull=True)
                ),
                name="authsession_subject_matches_audience",
            ),
        ]

    def __str__(self) -> str:
        return gettext("%(audience)s sessiyasi %(id)s") % {
            "audience": self.get_audience_display(),
            "id": self.pk,
        }


__all__ = ["AuthSession"]
