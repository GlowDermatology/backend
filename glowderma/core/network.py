import ipaddress
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

from glowderma.core.api.exceptions import error_body
from glowderma.core.clientip import has_internal_secret, peer_ip


class AdminNetworkMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path_info.startswith(
            ("/api/v1/admin/", "/api/v1/schema/", "/api/v1/docs/", "/django-admin/")
        ):
            try:
                peer = ipaddress.ip_address(peer_ip(request))
                allowed = any(peer in ipaddress.ip_network(net) for net in settings.ADMIN_API_ALLOWED_NETS)
            except ValueError:
                allowed = False
            if not allowed or (
                settings.ADMIN_API_REQUIRE_INTERNAL_SECRET and not has_internal_secret(request)
            ):
                return JsonResponse(error_body("permission_denied", request), status=403)
        return self.get_response(request)
