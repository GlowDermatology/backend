from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema


class StaffJWTScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "glowderma.accounts.authentication.StaffJWTAuthentication"
    name = "StaffJWT"

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, Any]:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
