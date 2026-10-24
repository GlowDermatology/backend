import hmac
import ipaddress
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse


def has_internal_secret(request: HttpRequest) -> bool:
    try:
        header = request.META.get("HTTP_X_GD_INTERNAL_SECRET", "").encode("latin-1")
    except UnicodeEncodeError:
        return False
    return bool(settings.INTERNAL_API_SECRET) and hmac.compare_digest(
        header, settings.INTERNAL_API_SECRET.encode()
    )


def peer_ip(request: HttpRequest) -> str:
    current = str(request.META.get("REMOTE_ADDR", ""))
    proxies = [ipaddress.ip_network(value) for value in settings.TRUSTED_PROXIES]
    hops = [hop.strip() for hop in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    while True:
        try:
            parsed = ipaddress.ip_address(current)
        except ValueError:
            return current
        if not any(parsed in network for network in proxies) or not hops:
            return str(parsed)
        candidate = hops.pop()
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return str(parsed)
        current = candidate


def client_ip(request: HttpRequest) -> str:
    if has_internal_secret(request):
        try:
            return str(ipaddress.ip_address(request.META.get("HTTP_X_GD_CLIENT_IP", "")))
        except ValueError:
            pass
    return peer_ip(request)


class ClientIPMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.client_ip = client_ip(request)  # type: ignore[attr-defined]
        return self.get_response(request)
