import hashlib
import math
from typing import Any

from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.cache import cache, caches
from django.core.cache.backends.redis import RedisCache
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse
from rest_framework.exceptions import Throttled

from glowderma.core import timeutils
from glowderma.core.api.exceptions import error_body


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _increment(key: str, timeout: int) -> int:
    backend = caches["default"]
    if isinstance(backend, RedisCache):
        raw_key = backend.make_and_validate_key(key)
        client = backend._cache.get_client(raw_key, write=True)
        with client.pipeline(transaction=True) as pipeline:
            pipeline.incr(raw_key)
            pipeline.expire(raw_key, timeout, nx=True)
            return int(pipeline.execute()[0])
    # LocMem's increment checks expiry while holding its lock and never creates a missing key.
    backend.add(key, 0, timeout=timeout)
    try:
        return int(backend.incr(key))
    except ValueError:
        if backend.add(key, 1, timeout=timeout):
            return 1
        return int(backend.incr(key))


class LoginGuard:
    def check(self, scope: str, hashed_email: str, *, reserve: bool = True) -> None:
        key = f"auth:lock:{scope}:{hashed_email}"
        expiry = cache.get(key)
        if expiry is not None:
            wait = math.ceil(float(expiry) - timeutils.now().timestamp())
            if wait > 0:
                raise Throttled(wait=wait)
            cache.delete(key)
        # Reserve an attempt before authentication so concurrent guesses share the limit.
        if reserve and _increment(f"auth:attempt:{scope}:{hashed_email}", 60) > 5:
            raise Throttled(wait=60)

    def record_failure(self, scope: str, hashed_email: str) -> None:
        count = _increment(f"auth:fail:{scope}:{hashed_email}", 60)
        if count == 5:
            locks = _increment(f"auth:lockn:{scope}:{hashed_email}", 24 * 60 * 60)
            wait = min(60 * 2 ** min(locks - 1, 4), 900)
            cache.set(f"auth:lock:{scope}:{hashed_email}", timeutils.now().timestamp() + wait, timeout=wait)

    def reset(self, scope: str, hashed_email: str) -> None:
        cache.delete(f"auth:fail:{scope}:{hashed_email}")
        cache.delete(f"auth:attempt:{scope}:{hashed_email}")


class GuardedAdminAuthenticationForm(AdminAuthenticationForm):
    def clean(self) -> dict[str, Any]:
        username = str(self.cleaned_data.get("username", "")).strip().lower()
        self.cleaned_data["username"] = username
        guard = LoginGuard()
        hashed = email_hash(username)
        guard.check("django_admin_login", hashed)
        try:
            result = super().clean()
        except ValidationError:
            guard.record_failure("django_admin_login", hashed)
            raise
        guard.reset("django_admin_login", hashed)
        return result


# Wired once at import time (config.urls imports this module); the view never mutates the site.
admin.site.login_form = GuardedAdminAuthenticationForm


def guarded_admin_login(request: HttpRequest, extra_context: dict[str, Any] | None = None) -> HttpResponse:
    try:
        return admin.site.login(request, extra_context=extra_context)
    except Throttled as exc:
        wait = getattr(exc, "wait", 0)
        response = JsonResponse(
            error_body(
                "throttled",
                request,
                message=f"Juda ko‘p urinish. {wait} soniyadan so‘ng qayta urinib ko‘ring.",
            ),
            status=429,
        )
        response["Retry-After"] = str(wait)
        return response
