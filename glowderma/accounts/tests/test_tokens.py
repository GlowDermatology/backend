from datetime import timedelta
from typing import Any

import jwt
import pytest
from django.conf import settings
from django.test import override_settings

from glowderma.accounts import services, tokens
from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils
from glowderma.core.api.exceptions import AuthFailed

pytestmark = pytest.mark.django_db


def claims() -> dict[str, Any]:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    token, _ = tokens.issue_access(session)
    return tokens.decode_access(token, audience="staff")


def test_access_token_roundtrip() -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    token, expires = tokens.issue_access(session)
    decoded = tokens.decode_access(token, audience="staff")
    assert decoded["sub"] == str(user.pk)
    assert decoded["sid"] == str(session.pk)
    assert decoded["iss"] == "glowderma"
    assert decoded["aud"] == "staff"
    assert {"iss", "aud", "sub", "sid", "iat", "exp", "jti"} == set(decoded)
    assert decoded["exp"] == int(expires.timestamp())


def test_access_exp_capped_at_session_expiry() -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    session.absolute_expires_at = timeutils.now() + timedelta(seconds=10)
    token, expires = tokens.issue_access(session)
    assert int(expires.timestamp()) == int(session.absolute_expires_at.timestamp())
    assert tokens.decode_access(token, audience="staff")["exp"] == int(expires.timestamp())


def test_decode_rejects_wrong_audience() -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    token, _ = tokens.issue_access(session)
    with pytest.raises(AuthFailed):
        tokens.decode_access(token, audience="customer")


def test_decode_rejects_unknown_kid() -> None:
    token = jwt.encode(claims(), b"x" * 32, algorithm="HS256", headers={"kid": "unknown"})
    with pytest.raises(AuthFailed):
        tokens.decode_access(token, audience="staff")


@pytest.mark.parametrize("algorithm", ["none", "HS512"])
def test_decode_rejects_alg_none_and_hs512(algorithm: str) -> None:
    token = jwt.encode(
        claims(),
        "" if algorithm == "none" else b"x" * 64,
        algorithm=algorithm,
        headers={"kid": settings.JWT_ACTIVE_KID},
    )
    with pytest.raises(AuthFailed):
        tokens.decode_access(token, audience="staff")


def test_key_rotation_old_kid_still_valid() -> None:
    old_key, new_key = b"a" * 32, b"b" * 32
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    with override_settings(JWT_SIGNING_KEYS={"old": old_key}, JWT_ACTIVE_KID="old"):
        token, _ = tokens.issue_access(session)
    with override_settings(JWT_SIGNING_KEYS={"old": old_key, "new": new_key}, JWT_ACTIVE_KID="new"):
        assert tokens.decode_access(token, audience="staff")["sub"] == str(user.pk)
