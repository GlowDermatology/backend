"""A second API worker process for ``test_revocation_across_workers`` (design spec "Verification":
revoked-session codes across gunicorn workers via Redis).

Run as ``python -m glowderma.accounts.tests.revocation_worker`` with the test settings, ``DATABASE_URL``
naming the test database and ``GD_WORKER_REDIS_URL`` / ``GD_WORKER_KEY_PREFIX`` naming the shared Redis
cache. It reads one JSON command per stdin line — ``{"audience": "staff" | "customer", "token": <access
JWT>, "headers": {<META>}, "no_db": bool}`` — authenticates it with the real authentication class of that
audience, exactly as a request on another gunicorn worker would, and answers one ``RESULT <json>`` line on
stdout: ``{"ok": <user id>}``, ``{"code": <401 code>}`` or ``{"error": "database"}`` when ``no_db`` cut the
database off and the check needed it. Other stdout lines are log output.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

RESULT_PREFIX = "RESULT "


class DatabaseUsed(Exception):
    """The worker reached for the database while ``no_db`` forbade it."""


def _refuse_database(*args: Any) -> Any:
    raise DatabaseUsed


def _authenticate(command: dict[str, Any]) -> dict[str, Any]:
    from django.db import connection
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from glowderma.accounts.authentication import CustomerJWTAuthentication, StaffJWTAuthentication
    from glowderma.core.api.exceptions import AuthFailed, PasswordChanged

    audience = command["audience"]
    request = Request(
        APIRequestFactory().get(
            "/api/v1/admin/auth/me/" if audience == "staff" else "/api/v1/me/",
            HTTP_AUTHORIZATION=f"Bearer {command['token']}",
            **command.get("headers", {}),
        )
    )
    backend = StaffJWTAuthentication() if audience == "staff" else CustomerJWTAuthentication()
    try:
        if command.get("no_db"):
            with connection.execute_wrapper(_refuse_database):
                result = backend.authenticate(request)
        else:
            result = backend.authenticate(request)
    except DatabaseUsed:
        return {"error": "database"}
    except PasswordChanged:
        return {"code": "password_changed"}
    except AuthFailed as exc:
        return {"code": str(exc.get_codes())}
    if result is None:
        return {"code": "anonymous"}
    return {"ok": result[0].pk}


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    import django

    django.setup()
    from django.test import override_settings

    caches = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ["GD_WORKER_REDIS_URL"],
            "KEY_PREFIX": os.environ["GD_WORKER_KEY_PREFIX"],
            "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
        }
    }
    with override_settings(CACHES=caches):
        print(RESULT_PREFIX + json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            if line.strip():
                print(RESULT_PREFIX + json.dumps(_authenticate(json.loads(line))), flush=True)


if __name__ == "__main__":
    main()
