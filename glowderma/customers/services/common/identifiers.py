"""Customer identifiers (hybrid identity: a phone or an e-mail address) and the login guard keys and
scopes."""

from __future__ import annotations

from glowderma.accounts.models import Customer
from glowderma.accounts.security.identifiers import email_throttle_ident, phone_throttle_ident

LOGIN_SCOPE = "customer_login"


LOGIN_DEVICE_SCOPE = "customer_login_device"


REAUTH_SCOPE = "customer_reauth"


def is_email(identifier: str) -> bool:
    """Hybrid identity: a validated identifier is an e-mail address when it holds ``@`` (a phone never)."""
    return "@" in identifier


def phone_guard_key(identifier: str) -> str:
    """The ``customer_login`` guard key of a phone or, given an address, of an e-mail (``pii_ref``)."""
    ident = email_throttle_ident(identifier) if is_email(identifier) else phone_throttle_ident(identifier)
    return ident or "invalid"


def customer_guard_key(customer_id: int) -> str:
    """The account-wide ``customer_login`` key: an account with a phone and an e-mail has one budget."""
    return f"customer:{customer_id}"


def target_kwargs(identifier: str) -> dict[str, str]:
    """``phone=`` or ``email=`` for the verification-token and customer lookups of ``identifier``."""
    return {"email": identifier} if is_email(identifier) else {"phone": identifier}


def customer_identifier(customer: Customer) -> str:
    """What the customer signs in with: the phone, else the e-mail address, else ``""`` (social only)."""
    return customer.phone or customer.email or ""


__all__ = [
    "LOGIN_DEVICE_SCOPE",
    "LOGIN_SCOPE",
    "REAUTH_SCOPE",
    "customer_guard_key",
    "customer_identifier",
    "is_email",
    "phone_guard_key",
    "target_kwargs",
]
