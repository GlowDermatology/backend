import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
from django.conf import settings

from glowderma.accounts.models import AuthSession
from glowderma.core import timeutils
from glowderma.core.api.exceptions import AuthFailed

# Claims every access token carries; customer tokens add ``rot`` (customer contract §1.2, §2.1.6).
REQUIRED_CLAIMS = ["iss", "aud", "sub", "sid", "iat", "exp", "jti"]
CUSTOMER_REQUIRED_CLAIMS = [*REQUIRED_CLAIMS, "rot"]


def access_ttl(audience: str) -> int:
    """Access JWT lifetime in seconds: ``STAFF_ACCESS_TTL`` or ``CUSTOMER_ACCESS_TTL``."""
    return int(settings.CUSTOMER_ACCESS_TTL if audience == "customer" else settings.STAFF_ACCESS_TTL)


def issue_access(session: AuthSession) -> tuple[str, datetime]:
    """An HS256 access JWT for ``session``, capped at the session's ``absolute_expires_at``.

    Customer tokens carry ``rot`` = ``AuthSession.rotation`` (the integer rotation counter, never a
    timestamp).
    """
    issued = int(timeutils.now().timestamp())
    expires = min(issued + access_ttl(session.audience), int(session.absolute_expires_at.timestamp()))
    claims: dict[str, Any] = {
        "iss": settings.JWT_ISSUER,
        "aud": session.audience,
        "sub": str(session.staff_id if session.audience == "staff" else session.customer_id),
        "sid": str(session.pk),
        "jti": uuid.uuid4().hex,
        "iat": issued,
        "exp": expires,
    }
    if session.audience == "customer":
        claims["rot"] = int(session.rotation)
    token = jwt.encode(
        claims,
        settings.JWT_SIGNING_KEYS[settings.JWT_ACTIVE_KID],
        algorithm="HS256",
        headers={"kid": settings.JWT_ACTIVE_KID},
    )
    return token, datetime.fromtimestamp(expires, tz=UTC)


def decode_access(token: str, *, audience: str) -> dict[str, Any]:
    required = CUSTOMER_REQUIRED_CLAIMS if audience == "customer" else REQUIRED_CLAIMS
    try:
        header = jwt.get_unverified_header(token)
        key = settings.JWT_SIGNING_KEYS[header["kid"]]
        claims = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            audience=audience,
            issuer=settings.JWT_ISSUER,
            options={"require": required, "strict_aud": True},
        )
        claims["sid"] = str(uuid.UUID(str(claims["sid"])))
        int(claims["sub"])
        if audience == "customer":
            rot = claims["rot"]
            if isinstance(rot, bool) or not isinstance(rot, int) or rot < 0:
                raise ValueError("rot must be a non-negative integer")
    except (jwt.PyJWTError, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise AuthFailed("not_authenticated") from exc
    return claims
