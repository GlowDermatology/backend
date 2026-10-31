import ipaddress
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound, JsonResponse
from django.http.request import split_domain_port

from glowderma.core.api.exceptions import error_body
from glowderma.core.clientip import has_internal_secret, peer_ip

# The staff API surfaces: only the Next container (ADMIN_API_ALLOWED_NETS) with the internal secret.
ADMIN_API_PREFIXES = ("/api/v1/admin/", "/api/v1/schema/", "/api/v1/docs/")
# The ops console (Unfold at /django-admin/, its login and 2FA steps); /static/ is never gated.
CONSOLE_PREFIX = "/django-admin"


def passes_admin_gate(request: HttpRequest) -> bool:
    """Whether the request would pass the admin network gate (contract §1): the network peer is inside
    ``ADMIN_API_ALLOWED_NETS`` and, where required (production), it carries the matching internal secret."""
    try:
        peer = ipaddress.ip_address(peer_ip(request))
        allowed = any(peer in net for net in settings.ADMIN_API_ALLOWED_NETS)
    except ValueError:
        allowed = False
    return allowed and (not settings.ADMIN_API_REQUIRE_INTERNAL_SECRET or has_internal_secret(request))


def passes_console_gate(request: HttpRequest) -> bool:
    """Whether the browser may reach the ops console: its address is inside ``OPS_ADMIN_ALLOWED_NETS``.

    The address is the TCP peer, or the ``X-Forwarded-For`` hop a peer inside ``TRUSTED_PROXIES`` (the edge,
    which overwrites the header) reports — never ``X-GD-Client-IP``: the Next BFF does not proxy the console,
    and an ``X-Forwarded-For`` sent by anyone else is ignored."""
    try:
        client = ipaddress.ip_address(peer_ip(request))
    except ValueError:
        return False
    return any(client in net for net in settings.OPS_ADMIN_ALLOWED_NETS)


def on_console_host(request: HttpRequest) -> bool:
    """Whether the request names the console's own host: always while ``OPS_DOMAIN`` is unset (local
    development, tests), else the ``Host`` (as ``ALLOWED_HOSTS`` validated it, port ignored) must be
    ``OPS_DOMAIN`` — the API's or the storefront's host never serves the console, even from an allowed
    address."""
    domain = str(getattr(settings, "OPS_DOMAIN", "") or "").strip().lower()
    if not domain:
        return True
    host, _port = split_domain_port(request.get_host())
    return host == domain


class AdminNetworkMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info
        if path.startswith(CONSOLE_PREFIX):
            # Like the edge: a browser outside the allowed networks, or on another host, never learns the
            # console exists.
            if not on_console_host(request) or not passes_console_gate(request):
                return HttpResponseNotFound(b"")
        elif path.startswith(ADMIN_API_PREFIXES):
            if not passes_admin_gate(request):
                return JsonResponse(error_body("permission_denied", request), status=403)
        return self.get_response(request)
