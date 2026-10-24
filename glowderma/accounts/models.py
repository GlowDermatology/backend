import uuid
from datetime import datetime
from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models.functions import Lower

from glowderma.core import timeutils

from .managers import StaffUserManager


def date_joined_default() -> datetime:
    return timeutils.now()


class StaffUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=10, choices=[("admin", "admin")], default="admin")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=date_joined_default)
    objects = StaffUserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="staffuser_email_ci_unique")]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)


class AuthSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audience = models.CharField(max_length=16, choices=[("staff", "staff"), ("customer", "customer")])
    staff = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="sessions"
    )
    customer_id = models.BigIntegerField(null=True)
    device_label = models.CharField(max_length=100)
    refresh_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=date_joined_default, editable=False)
    last_used_at = models.DateTimeField(default=date_joined_default)
    absolute_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    revoke_reason = models.CharField(
        max_length=24,
        blank=True,
        choices=[
            (value, value)
            for value in ("logout", "expired", "password_changed", "deactivated", "replaced", "admin_revoked")
        ],
    )

    def __str__(self) -> str:
        return f"{self.audience}: {self.pk}"


class SocialIdentity(models.Model):
    staff = models.ForeignKey(
        "accounts.StaffUser", on_delete=models.CASCADE, related_name="social_identities"
    )
    provider = models.CharField(max_length=16, choices=[("google", "google"), ("facebook", "facebook")])
    subject = models.CharField(max_length=254)
    email_at_link = models.EmailField(max_length=254, blank=True)
    created_at = models.DateTimeField(default=date_joined_default, editable=False)
    last_login_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "subject"], name="social_provider_subject_unique"),
            models.UniqueConstraint(fields=["staff", "provider"], name="social_staff_provider_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.provider}: {self.subject}"
