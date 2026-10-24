import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
from django.conf import settings

from glowderma.accounts.models import AuthSession
from glowderma.core import timeutils
from glowderma.core.api.exceptions import AuthFailed


def issue_access(session: AuthSession) -> tuple[str, datetime]:
    issued = int(timeutils.now().timestamp())
    expires = min(issued + settings.STAFF_ACCESS_TTL, int(session.absolute_expires_at.timestamp()))
    claims = {
        "iss": settings.JWT_ISSUER,
        "aud": session.audience,
        "sub": str(session.staff_id if session.audience == "staff" else session.customer_id),
        "sid": str(session.pk),
        "jti": uuid.uuid4().hex,
        "iat": issued,
        "exp": expires,
    }
    token = jwt.encode(
        claims,
        settings.JWT_SIGNING_KEYS[settings.JWT_ACTIVE_KID],
        algorithm="HS256",
        headers={"kid": settings.JWT_ACTIVE_KID},
    )
    return token, datetime.fromtimestamp(expires, tz=UTC)


def decode_access(token: str, *, audience: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
        key = settings.JWT_SIGNING_KEYS[header["kid"]]
        claims = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            audience=audience,
            issuer=settings.JWT_ISSUER,
            options={"require": ["iss", "aud", "sub", "sid", "iat", "exp", "jti"], "strict_aud": True},
        )
        claims["sid"] = str(uuid.UUID(str(claims["sid"])))
        int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise AuthFailed("not_authenticated") from exc
    return claims
