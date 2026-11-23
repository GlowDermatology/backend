"""The accounts models, one subject per module; this facade is what Django loads as ``accounts.models``.

Every model must be imported here: Django registers the models of an app by importing ``<app>.models``.
Callers keep importing ``from glowderma.accounts.models import X``. Migrations reference
``glowderma.accounts.models.date_joined_default``, which resolves through this facade to the same object.
"""

from glowderma.accounts.models.auth_sessions import AuthSession
from glowderma.accounts.models.customers import Address, Customer, CustomerSocialIdentity
from glowderma.accounts.models.defaults import PHONE_E164_REGEX, date_joined_default
from glowderma.accounts.models.devices import Device
from glowderma.accounts.models.otp_codes import SmsCode
from glowderma.accounts.models.staff import SocialIdentity, StaffPasswordToken, StaffUser, StaffUserManager

__all__ = [
    "PHONE_E164_REGEX",
    "Address",
    "AuthSession",
    "Customer",
    "CustomerSocialIdentity",
    "Device",
    "SmsCode",
    "SocialIdentity",
    "StaffPasswordToken",
    "StaffUser",
    "StaffUserManager",
    "date_joined_default",
]
