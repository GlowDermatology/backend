"""Staff second factors: one TOTP device per staff member and *scope* plus ten one-time recovery codes.

Two scopes share these functions (feat/admin-ux) and never see each other's rows: :data:`CONSOLE`, the ops
console's factor (enrolled inside a manager-opened window, ``glowderma.accounts.console.access``), and
:data:`PANEL`, the admin panel's optional TOTP (``glowderma.accounts.mfa``, each person's own choice). A
scope is a namespace of ``Device.name``: the panel's devices are named :data:`PANEL_DEVICE_NAME` /
:data:`PANEL_RECOVERY_DEVICE_NAME`; the console's are every other device (named :data:`DEVICE_NAME` /
:data:`RECOVERY_DEVICE_NAME` since enrolment began, so a console row from before the panel scope existed
keeps working). Every helper takes ``scope`` (keyword-only, required): the panel can never confirm, use,
count or delete a console device, and the console never accepts a panel device or recovery code, so the
console's enrolment window and four-eyes rule cannot be bypassed from the panel.

The package is a facade over ``scopes`` (the namespaces and stored device names), ``totp``
(enrolment and verification), ``provisioning`` (key, URI, QR code), ``recovery_codes`` and ``reset``.

TOTP (django-otp ``TOTPDevice``): 30 s steps, 6 digits, ±1 step of tolerance. A verified code moves the
device's ``last_t`` forward, so the same code (or an older one) is refused afterwards: no replay inside its
window. Enrolment creates an unconfirmed device whose secret is shown once (QR + base32); the first valid code
confirms it, and only a confirmed device ever verifies a session (``OTPMiddleware`` ignores the others).

Recovery codes (django-otp ``StaticDevice``/``StaticToken``): otp_static stores its tokens in plain text and
``StaticDevice.verify_token`` compares them as stored, so this module never uses that method. A
``StaticToken.token`` holds ``recovery_hash(code)`` instead — HMAC-SHA256 keyed by ``SECRET_KEY`` (salted
for this purpose), base32, the first 16 characters (80 bits: the column is ``max_length=16``) — and a code is
checked by recomputing that hash (also under each ``SECRET_KEY_FALLBACKS`` key, so a key rotation keeps the
codes) and deleting the matching row. No schema change: a database dump alone reveals no usable code, and the
stock ``addstatictoken`` command's plain tokens never match a hash. Codes are ten base32 characters (50
bits), shown once as ``xxxxx-xxxxx``; the code step's lockout bounds online guessing.

Accepted risk: the TOTP seed (``TOTPDevice.key``) stays in plain text — django-otp reads the column directly
to compute codes, and encrypting it would mean forking its device model. A database dump plus a known password
can therefore produce codes; after a suspected leak every console user gets ``ops_access reset-2fa``
(docs/ops/staff-access.md "Accepted risks").
"""

from glowderma.accounts.second_factor.provisioning import manual_key, otpauth_uri, qr_svg
from glowderma.accounts.second_factor.recovery_codes import (
    RECOVERY_ALPHABET,
    RECOVERY_CODE_COUNT,
    RECOVERY_LENGTH,
    RECOVERY_SALT,
    RecoveryUse,
    issue_recovery_codes,
    normalize_recovery_code,
    recovery_codes_left,
    recovery_hash,
    use_recovery_code,
)
from glowderma.accounts.second_factor.reset import reset_devices
from glowderma.accounts.second_factor.scopes import (
    CONSOLE,
    DEVICE_NAME,
    EXCLUSIVE_RECOVERY_NAMES,
    EXCLUSIVE_TOTP_NAMES,
    PANEL,
    PANEL_DEVICE_NAME,
    PANEL_ISSUER,
    PANEL_RECOVERY_DEVICE_NAME,
    RECOVERY_DEVICE_NAME,
    DeviceScope,
)
from glowderma.accounts.second_factor.totp import (
    TOTP_CODE,
    Enrolment,
    confirm_enrolment,
    confirmed_totp,
    enrolment_device,
    has_second_factor,
    verify_totp,
)

__all__ = [
    "CONSOLE",
    "DEVICE_NAME",
    "EXCLUSIVE_RECOVERY_NAMES",
    "EXCLUSIVE_TOTP_NAMES",
    "PANEL",
    "PANEL_DEVICE_NAME",
    "PANEL_ISSUER",
    "PANEL_RECOVERY_DEVICE_NAME",
    "RECOVERY_ALPHABET",
    "RECOVERY_CODE_COUNT",
    "RECOVERY_DEVICE_NAME",
    "RECOVERY_LENGTH",
    "RECOVERY_SALT",
    "TOTP_CODE",
    "DeviceScope",
    "Enrolment",
    "RecoveryUse",
    "confirm_enrolment",
    "confirmed_totp",
    "enrolment_device",
    "has_second_factor",
    "issue_recovery_codes",
    "manual_key",
    "normalize_recovery_code",
    "otpauth_uri",
    "qr_svg",
    "recovery_codes_left",
    "recovery_hash",
    "reset_devices",
    "use_recovery_code",
    "verify_totp",
]
