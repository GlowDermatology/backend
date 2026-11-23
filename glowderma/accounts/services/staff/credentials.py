"""Staff password credentials (admin contract §2.16; feat/admin-ux overrides GST-3): temporary passwords, the
choice between a password and an e-mailed link, credential take-over rules and the lockout reset.
"""

from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework.exceptions import ValidationError

from glowderma.accounts.models import StaffUser
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.accounts.services.staff import messages
from glowderma.core import enums, integration_modes
from glowderma.core.api.exceptions import field_error

logger = logging.getLogger("glowderma.accounts.services")


# Login lockout scopes a password reset ends (``LoginView`` and the guarded Django admin login form).
STAFF_LOGIN_SCOPES = ("login", "django_admin_login")
# A temporary password: four groups of four characters without look-alikes (0/O, 1/l/I), ~92 bits.
TEMPORARY_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"  # noqa: S105 - alphabet
TEMPORARY_PASSWORD_GROUPS = 4
TEMPORARY_PASSWORD_ATTEMPTS = 20


def generate_temporary_password(user: StaffUser) -> str:
    """A random ``xxxx-xxxx-xxxx-xxxx`` password that passes ``AUTH_PASSWORD_VALIDATORS`` for ``user``."""
    for _ in range(TEMPORARY_PASSWORD_ATTEMPTS):
        candidate = "-".join(
            "".join(secrets.choice(TEMPORARY_PASSWORD_ALPHABET) for _ in range(4))
            for _ in range(TEMPORARY_PASSWORD_GROUPS)
        )
        try:
            validate_password(candidate, user)
        except DjangoValidationError:  # pragma: no cover - ~92 random bits are never common or similar
            continue
        return candidate
    raise RuntimeError(
        "Could not generate a temporary password that passes the validators"
    )  # pragma: no cover


def superuser_may_be_taken_by(user: StaffUser, actor: StaffUser | None) -> bool:
    """Whether ``actor`` may take over a credential of ``user`` (learn its password, strip its MFA):
    anyone for an ordinary account, only another superuser for a superuser (review SEC-2; the operator's
    commands are the break-glass path)."""
    return not user.is_superuser or (actor is not None and actor.is_superuser)


def unlock_on_commit(email: str, scopes: tuple[str, ...] = STAFF_LOGIN_SCOPES) -> None:
    """End the lockouts of ``email`` in ``scopes`` (the login ones by default) once the surrounding
    transaction commits (a rolled-back reset leaves them). Best effort: a Redis error logs and never fails
    the committed reset."""
    hashed = email_hash(email)

    def unlock() -> None:
        guard = LoginGuard()
        for scope in scopes:
            try:
                guard.unlock(scope, hashed)
            except Exception:
                logger.exception("auth.unlock_failed scope=%s", scope)

    transaction.on_commit(unlock, robust=True)


def staff_email_invite_available() -> bool:
    """``Me.capabilities.staff_email_invite``: e-mail links can be sent — ``ADMIN_PANEL_URL`` is set and
    e-mail does not run its fake adapter under the production settings (C§6.14)."""
    return bool(settings.ADMIN_PANEL_URL) and not integration_modes.fake_in_production("email")


def check_delivery(delivery: str) -> None:
    if delivery not in enums.StaffPasswordDelivery.values:
        raise ValueError(f"Unknown delivery {delivery!r}")
    if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD and not staff_email_invite_available():
        raise ValidationError(
            {"delivery": [field_error("invalid_choice", messages.STAFF_EMAIL_INVITE_UNAVAILABLE)]}
        )


def new_password(user: StaffUser, password: str | None, delivery: str) -> str | None:
    """The operator's ``password``; else ``None`` (unusable) for an e-mail link alone; else a generated
    one."""
    if password is not None:
        return password
    if delivery == enums.StaffPasswordDelivery.EMAIL_LINK:
        return None
    return generate_temporary_password(user)


def must_change(password: str | None, must_change: bool | None) -> bool:
    """Someone else chose the password (generated, or none yet): the person sets their own first."""
    return must_change if must_change is not None else password is None


__all__ = [
    "STAFF_LOGIN_SCOPES",
    "TEMPORARY_PASSWORD_ALPHABET",
    "TEMPORARY_PASSWORD_GROUPS",
    "TEMPORARY_PASSWORD_ATTEMPTS",
    "generate_temporary_password",
    "staff_email_invite_available",
    "check_delivery",
    "new_password",
    "must_change",
    "superuser_may_be_taken_by",
    "unlock_on_commit",
]
