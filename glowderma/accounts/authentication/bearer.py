"""The ``Authorization: Bearer <token>`` header both JWT audiences read."""

from __future__ import annotations

from rest_framework.authentication import get_authorization_header
from rest_framework.request import Request

from glowderma.core.api.exceptions import AuthFailed


def bearer_token(request: Request) -> str | None:
    header = get_authorization_header(request).split()
    if not header or header[0].lower() != b"bearer":
        return None
    if len(header) != 2:
        raise AuthFailed("not_authenticated")
    try:
        return header[1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise AuthFailed("not_authenticated") from exc


__all__ = [
    "bearer_token",
]
