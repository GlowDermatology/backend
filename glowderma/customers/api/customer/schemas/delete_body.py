"""OpenAPI helpers of the customer views.

drf-spectacular documents a request body only for ``PUT``/``PATCH``/``POST``. ``DELETE me/`` and ``DELETE
me/social/{provider}/`` carry the re-authentication as a JSON body (customer contract §2.0 row 16, §2.2.3,
§2.2.8), so their views use :class:`DeleteBodyAutoSchema`, which documents the declared request serializer
for ``DELETE`` too, as a required body.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.openapi import AutoSchema


class DeleteBodyAutoSchema(AutoSchema):
    def _get_request_body(self, direction: str = "request") -> Any:
        if self.method != "DELETE":
            return super()._get_request_body(direction)  # type: ignore[no-untyped-call]
        self.method = "POST"  # the base class answers None for any other method
        try:
            body = super()._get_request_body(direction)  # type: ignore[no-untyped-call]
        finally:
            self.method = "DELETE"
        if body:
            body["required"] = True
        return body
