import re
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from glowderma.core.clientip import has_internal_secret
from glowderma.core.logging import bind_request_id, unbind_request_id


class RequestIdMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        supplied = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = (
            supplied
            if has_internal_secret(request) and re.fullmatch(r"[A-Za-z0-9-]{8,64}", supplied)
            else str(uuid.uuid4())
        )
        request.request_id = request_id  # type: ignore[attr-defined]
        token = bind_request_id(request_id)
        try:
            response = self.handle(request, request_id)
        finally:
            unbind_request_id(token)
        response["X-Request-ID"] = request_id
        if request.path_info.startswith("/api/"):
            response.setdefault("Cache-Control", "no-store")
            response["Content-Language"] = "uz"
        return response

    def handle(self, request: HttpRequest, request_id: str) -> HttpResponse:
        try:
            too_large = int(request.META.get("CONTENT_LENGTH") or 0) > 6 * 1024 * 1024
        except ValueError:
            too_large = False
        if too_large:
            return JsonResponse(
                {
                    "code": "payload_too_large",
                    "message": "Fayl juda katta.",
                    "errors": [],
                    "request_id": request_id,
                },
                status=413,
            )
        return self.get_response(request)
