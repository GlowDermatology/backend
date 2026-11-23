"""DRF authentication of ``accounts`` behind one facade: the staff and customer access JWTs and the customer
app's device headers. ``SPECTACULAR_SETTINGS["SERVE_AUTHENTICATION"]`` and the ``accounts.schema`` extensions
name ``glowderma.accounts.authentication.<Class>``, which resolves here to the defining class itself.

Submodules: ``bearer`` (the bearer header), ``device_headers`` (validation and the minimum-version gate),
``staff_jwt`` and ``customer_jwt``.
"""

from glowderma.accounts.authentication.bearer import bearer_token
from glowderma.accounts.authentication.customer_jwt import (
    DEVICE_BUMP_SECONDS,
    MISMATCH_AUDIT_SECONDS,
    CustomerJWTAuthentication,
    OptionalCustomerAuthentication,
)
from glowderma.accounts.authentication.device_headers import (
    APP_VERSION_PATTERN,
    FID_PATTERN,
    PLATFORMS,
    REQUEST_DEVICE_ATTR,
    DeviceHeaders,
    min_app_version,
    parse_device_headers,
    request_device,
)
from glowderma.accounts.authentication.staff_jwt import StaffJWTAuthentication

__all__ = [
    "APP_VERSION_PATTERN",
    "DEVICE_BUMP_SECONDS",
    "FID_PATTERN",
    "MISMATCH_AUDIT_SECONDS",
    "PLATFORMS",
    "REQUEST_DEVICE_ATTR",
    "CustomerJWTAuthentication",
    "DeviceHeaders",
    "OptionalCustomerAuthentication",
    "StaffJWTAuthentication",
    "bearer_token",
    "min_app_version",
    "parse_device_headers",
    "request_device",
]
