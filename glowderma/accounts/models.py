from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models.functions import Lower

from glowderma.core import timeutils

from .managers import StaffUserManager


class StaffUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=10, choices=[("admin", "admin")], default="admin")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timeutils.now)
    objects = StaffUserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="staffuser_email_ci_unique")]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)
