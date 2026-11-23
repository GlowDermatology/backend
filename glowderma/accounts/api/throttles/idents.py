"""Who a customer-facing request counts against (customer contract §1.8): carrier-grade NAT puts thousands of
app users behind a few IPs, so customer throttles key on the device first (``pii_ref`` of the fid, never the
raw value), the signed-in customer, or the website BFF's own calls, keeping a high pure-IP ceiling as a
backstop."""

from __future__ import annotations

from rest_framework.request import Request

from glowderma.accounts.authentication import FID_PATTERN
from glowderma.accounts.models import Customer
from glowderma.core.clientip import forwarded_client_ip, has_internal_secret
from glowderma.core.pii import pii_ref


def device_ref(request: Request) -> str | None:
    """``pii_ref`` of a well-formed ``X-GD-Device-Id`` (else ``None``)."""
    fid = str(request.headers.get("X-GD-Device-Id", ""))
    return pii_ref(fid) if FID_PATTERN.fullmatch(fid) else None


def is_customer(request: Request) -> bool:
    return isinstance(getattr(request, "user", None), Customer)


def bff_own_call(request: Request) -> bool:
    """B-W1: the website's BFF calling with a valid ``X-GD-Internal-Secret`` but no forwarded browser IP — a
    server-side read on its own behalf (cache revalidation, sitemap). Such calls count against
    ``public_bff`` instead of the per-device / per-IP scopes, which would otherwise pool every visitor's
    page renders on the web server's one IP. With a forwarded ``X-GD-Client-IP`` (the same trust rule as
    ``core.clientip``) the ordinary scopes key on the browser's IP."""
    django_request = request._request
    return has_internal_secret(django_request) and forwarded_client_ip(django_request) is None


__all__ = ["bff_own_call", "device_ref", "is_customer"]
