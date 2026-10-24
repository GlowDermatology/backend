from typing import Any

from django.core.cache import cache
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.request import Request

from glowderma.accounts import services, tokens
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.core import timeutils
from glowderma.core.api.exceptions import AuthFailed


class StaffJWTAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[StaffUser, dict[str, Any]] | None:
        header = get_authorization_header(request).split()
        if not header or header[0].lower() != b"bearer":
            return None
        if len(header) != 2:
            raise AuthFailed("not_authenticated")
        try:
            raw_token = header[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuthFailed("not_authenticated") from exc
        claims = tokens.decode_access(raw_token, audience="staff")
        key = f"auth:sid:{claims['sid']}"
        value = cache.get(key)
        if value is None:
            session = AuthSession.objects.select_related("staff").filter(pk=claims["sid"]).first()
            if session is None:
                raise AuthFailed("SESSION_EXPIRED")
            if session.audience != "staff" or session.staff_id != int(claims["sub"]):
                raise AuthFailed("not_authenticated")
            services.check_session(session)
            remaining = session.absolute_expires_at.timestamp() - timeutils.now().timestamp()
            active = f"active:{session.absolute_expires_at.timestamp()}"
            cache.add(key, active, timeout=max(1, min(60, int(remaining))))
            value = cache.get(key, active)
        if isinstance(value, str) and value.startswith("revoked:"):
            raise AuthFailed("SESSION_REPLACED" if value == "revoked:replaced" else "SESSION_EXPIRED")
        try:
            if not isinstance(value, str) or not value.startswith("active:"):
                raise ValueError("Invalid session cache entry")
            absolute = float(value.removeprefix("active:"))
            if timeutils.now().timestamp() >= absolute:
                raise AuthFailed("SESSION_EXPIRED")
        except (ValueError, TypeError) as exc:
            raise AuthFailed("SESSION_EXPIRED") from exc
        user = StaffUser.objects.filter(pk=int(claims["sub"])).first()
        if user is None or not user.is_active:
            raise AuthFailed("SESSION_EXPIRED")
        return user, claims

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="api"'
