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
    proxies = settings.TRUSTED_PROXIES
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


def forwarded_client_ip(request: HttpRequest) -> str | None:
    """The end user's IP a trusted BFF forwarded (``X-GD-Client-IP`` with a valid ``X-GD-Internal-Secret``),
    else ``None`` (no secret, or the BFF called on its own behalf without a well-formed client IP)."""
    if not has_internal_secret(request):
        return None
    try:
        return str(ipaddress.ip_address(request.META.get("HTTP_X_GD_CLIENT_IP", "")))
    except ValueError:
        return None


def client_ip(request: HttpRequest) -> str:
    return forwarded_client_ip(request) or peer_ip(request)


# Longest user agent kept (longer ones are cut); control characters are dropped.
USER_AGENT_MAX = 512


def client_user_agent(request: HttpRequest) -> str:
    """The end user's browser or app user agent (B-W1 session list): the website's BFF forwards the
    browser's as ``X-GD-Client-UA``, honoured only together with a valid ``X-GD-Internal-Secret`` (like
    ``X-GD-Client-IP``); otherwise the request's own ``User-Agent``. Printable characters only, at most
    ``USER_AGENT_MAX``."""
    raw = ""
    if has_internal_secret(request):
        raw = str(request.META.get("HTTP_X_GD_CLIENT_UA", ""))
    if not raw:
        raw = str(request.META.get("HTTP_USER_AGENT", ""))
    return "".join(char for char in raw if char.isprintable())[:USER_AGENT_MAX].strip()


class ClientIPMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.client_ip = client_ip(request)  # type: ignore[attr-defined]
        return self.get_response(request)
