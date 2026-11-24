"""What the customer sign-in services answer: a session, or the next step (verify the device, the phone), and
the device context a sign-in starts from."""

from __future__ import annotations

from dataclasses import dataclass

from glowderma.accounts import services as account_services


@dataclass(frozen=True, slots=True)
class SignIn:
    """A successful sign-in: the new session (``AuthTokens``) and the customer it belongs to."""

    started: account_services.SessionStart
    customer_id: int
    is_new_customer: bool = False
    reactivated: bool = False


@dataclass(frozen=True, slots=True)
class DeviceStep:
    """``NextStep`` ``verify_device`` (customer contract §2.1.4 step 6)."""

    issue: account_services.CodeIssue


@dataclass(frozen=True, slots=True)
class PhoneStep:
    """``NextStep`` ``verify_phone`` (customer contract §2.1.8): the signed ticket bound to this install."""

    ticket: str
    provider: str
    email: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class DeviceContext:
    """The device headers of the request and its client IP / language."""

    fid: str
    platform: str
    app_version: str
    ip: str = ""
    lang: str = "uz"


__all__ = [
    "DeviceContext",
    "DeviceStep",
    "PhoneStep",
    "SignIn",
]
