from typing import TYPE_CHECKING, Any

from django.contrib.auth.base_user import BaseUserManager


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


if TYPE_CHECKING:
    from .models import StaffUser
