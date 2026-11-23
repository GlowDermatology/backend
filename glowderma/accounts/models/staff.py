"""Staff accounts: the workforce user model, its password-set links and linked social identities."""

from __future__ import annotations

from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext

from glowderma.accounts.models.defaults import date_joined_default
from glowderma.core import enums
from glowderma.core.models import timestamp_default


class StaffUserManager(BaseUserManager["StaffUser"]):
    def create_user(self, email: str, password: str | None = None, **extra: Any) -> StaffUser:
        if not email.strip():
            raise ValueError("Email is required")
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> StaffUser:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_manager", True)  # a superuser always manages staff (accounts.0008, GST-1)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True")
        return self.create_user(email, password, **extra)


class StaffUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True, verbose_name="email")
    full_name = models.CharField(max_length=150, blank=True, verbose_name="to‘liq ism")
    role = models.CharField(
        max_length=10, choices=enums.StaffRole.choices, default=enums.StaffRole.ADMIN, verbose_name="rol"
    )
    is_active = models.BooleanField(default=True, verbose_name="faol")
    is_staff = models.BooleanField(default=False, verbose_name="konsolga kirish")
    # Admin contract §2.16: only a manager creates, edits, deactivates, reactivates staff or resets their
    # password. Every other staff right is the same for everyone (the client's single "admin" role).
    is_manager = models.BooleanField(default=False, verbose_name="menejer")
    date_joined = models.DateTimeField(default=date_joined_default, verbose_name="qo‘shilgan")
    # feat/admin-ux (admin contract §2.1): an account whose password someone else chose (a generated temporary
    # password, an e-mail link) must set its own before the admin API answers anything else
    # (``IsActiveStaff``: 403 ``password_change_required``). db_default: a writer of the previous release.
    must_change_password = models.BooleanField(
        default=False, db_default=False, verbose_name="parolni almashtirishi shart"
    )
    password_changed_at = models.DateTimeField(null=True, blank=True, verbose_name="parol o‘zgargan")
    # The admin panel's language (uz / ru / en), set by ``PATCH auth/me/``; the e-mail links use it too.
    language = models.CharField(
        max_length=2,
        choices=enums.ContentLanguage.choices,
        default=enums.ContentLanguage.UZ,
        db_default=enums.ContentLanguage.UZ,
        verbose_name="til",
    )
    objects = StaffUserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        verbose_name = "xodim"
        verbose_name_plural = "xodimlar"
        constraints = [
            models.UniqueConstraint(Lower("email"), name="staffuser_email_ci_unique"),
            models.CheckConstraint(
                condition=~Q(role=enums.StaffRole.SPECIALIST)
                | Q(is_manager=False, is_superuser=False, is_staff=False),
                name="staffuser_specialist_has_no_admin_flags",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)


class StaffPasswordToken(models.Model):
    """A single-use link that lets a staff member set their own password (feat/admin-ux, admin contract
    §2.1 ``auth/password-token/*``): an invite of a new account or a reset. Only the SHA-256 of the raw token
    is stored; the raw value exists in the e-mail (and, sealed, in its outbox payload until it is sent).
    Issuing a new link revokes the open ones; using one, changing the password, a temporary-password reset
    and a deactivation revoke them too. Not shown in the ops console."""

    staff = models.ForeignKey(
        "accounts.StaffUser", on_delete=models.CASCADE, related_name="password_tokens", verbose_name="xodim"
    )
    purpose = models.CharField(
        max_length=8, choices=enums.StaffPasswordPurpose.choices, verbose_name="maqsad"
    )
    token_hash = models.CharField(max_length=64, unique=True, verbose_name="token xeshi")
    created_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yaratgan",
    )
    created_at = models.DateTimeField(default=timestamp_default, verbose_name="yaratilgan")
    expires_at = models.DateTimeField(verbose_name="amal qilish muddati")
    # handed to the e-mail relay by the ``email.staff_password_link`` handler (null: queued, or not sent)
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="yuborilgan")
    used_at = models.DateTimeField(null=True, blank=True, verbose_name="ishlatilgan")
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="bekor qilingan")

    class Meta:
        verbose_name = "parol havolasi"
        verbose_name_plural = "parol havolalari"
        indexes = [models.Index(fields=["staff", "created_at"], name="accounts_pwtoken_staff_created")]

    def __str__(self) -> str:
        return gettext("%(purpose)s havolasi · xodim #%(staff)s") % {
            "purpose": self.get_purpose_display(),
            "staff": self.staff_id,
        }


class SocialIdentity(models.Model):
    staff = models.ForeignKey(
        "accounts.StaffUser", on_delete=models.CASCADE, related_name="social_identities", verbose_name="xodim"
    )
    provider = models.CharField(
        max_length=16, choices=[("google", "Google"), ("facebook", "Facebook")], verbose_name="provayder"
    )
    subject = models.CharField(max_length=254, verbose_name="subyekt")
    email_at_link = models.EmailField(max_length=254, blank=True, verbose_name="bog‘langandagi email")
    created_at = models.DateTimeField(default=date_joined_default, editable=False, verbose_name="yaratilgan")
    last_login_at = models.DateTimeField(null=True, verbose_name="oxirgi kirish")

    class Meta:
        verbose_name = "xodim ijtimoiy kirishi"
        verbose_name_plural = "xodim ijtimoiy kirishlari"
        constraints = [
            models.UniqueConstraint(fields=["provider", "subject"], name="social_provider_subject_unique"),
            models.UniqueConstraint(fields=["staff", "provider"], name="social_staff_provider_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.get_provider_display()}: {self.subject}"


__all__ = ["SocialIdentity", "StaffPasswordToken", "StaffUser", "StaffUserManager"]
