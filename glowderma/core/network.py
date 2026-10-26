import ipaddress
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

from glowderma.core.api.exceptions import error_body
from glowderma.core.clientip import has_internal_secret, peer_ip


def passes_admin_gate(request: HttpRequest) -> bool:
    """Whether the request would pass the admin network gate (contract §1): the network peer is inside
    ``ADMIN_API_ALLOWED_NETS`` and, where required (production), it carries the matching internal secret."""
    try:
        peer = ipaddress.ip_address(peer_ip(request))
        allowed = any(peer in net for net in settings.ADMIN_API_ALLOWED_NETS)
    except ValueError:
        allowed = False
    return allowed and (not settings.ADMIN_API_REQUIRE_INTERNAL_SECRET or has_internal_secret(request))


class AdminNetworkMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path_info.startswith(
            ("/api/v1/admin/", "/api/v1/schema/", "/api/v1/docs/", "/django-admin/")
        ):
            if not passes_admin_gate(request):
                return JsonResponse(error_body("permission_denied", request), status=403)
        return self.get_response(request)
