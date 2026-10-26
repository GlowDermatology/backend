from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema


class StaffJWTScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "glowderma.accounts.authentication.StaffJWTAuthentication"
    name = "StaffJWT"

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, Any]:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


class CustomerJWTScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    """Customer access JWT (``aud = customer``); also covers ``OptionalCustomerAuthentication`` (O routes,
    where drf-spectacular adds the anonymous alternative because the permission is ``AllowAny``)."""

    target_class = "glowderma.accounts.authentication.CustomerJWTAuthentication"
    match_subclasses = True
    name = "CustomerJWT"

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, Any]:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
