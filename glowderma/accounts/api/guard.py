"""The staff login lockouts (``LoginGuard``) and the ops console's password step.

``LoginGuard`` is the per-account ladder (five failures a minute lock the account for 1, 2, 4, 8, then 15
minutes). A scope can add a long-window bound (``streak_limit``, the admin panel's TOTP codes): every failure
also counts in a consecutive-failure streak that only a success (or an unlock) clears and that lives
:data:`STREAK_TTL_SECONDS` after the last failure; each ``streak_limit``-th failure in a row locks the account
for 15 minutes, 1, 4, 16, then 24 hours, so a guesser who stays under five a minute is stopped too.

``IpFailureBudget`` adds a per-address budget to the console's sign-in steps: at most ``OPS_IP_FAILURE_LIMIT``
failed attempts per ``OPS_IP_FAILURE_WINDOW_SECONDS`` from one client address (an IPv6 client by its /64),
counted separately for the password step and for the code steps; an attempt is reserved before it is checked
(concurrent guesses share the budget) and given back when it succeeds. Beyond the budget the address is
refused (429) until the window ends, audited ``console.ip_locked`` and alerted.
"""

import hashlib
import ipaddress
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import LoginView
from django.core.cache import cache, caches
from django.core.cache.backends.redis import RedisCache
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext, ngettext
from rest_framework.exceptions import Throttled
from unfold.forms import AuthenticationForm as UnfoldAuthenticationForm

from glowderma.accounts.console import events, state, twofactor
from glowderma.core import alerts, timeutils
from glowderma.core.clientip import peer_ip

logger = logging.getLogger(__name__)


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _increment(key: str, timeout: int, *, sliding: bool = False) -> int:
    """Add one to the counter ``key`` and return it. Its ``timeout`` runs from the first increment (a fixed
    window), or with ``sliding`` from the latest one."""
    backend = caches["default"]
    if isinstance(backend, RedisCache):
        raw_key = backend.make_and_validate_key(key)
        client = backend._cache.get_client(raw_key, write=True)
        with client.pipeline(transaction=True) as pipeline:
            pipeline.incr(raw_key)
            if sliding:
                pipeline.expire(raw_key, timeout)
            else:
                pipeline.expire(raw_key, timeout, nx=True)
            return int(pipeline.execute()[0])
    # LocMem's increment checks expiry while holding its lock and never creates a missing key.
    backend.add(key, 0, timeout=timeout)
    try:
        count = int(backend.incr(key))
    except ValueError:
        if backend.add(key, 1, timeout=timeout):
            return 1
        count = int(backend.incr(key))
    if sliding:
        backend.touch(key, timeout)
    return count


# The long-window bound of ``LoginGuard.record_failure(streak_limit=…)`` (module doc): the streak outlives the
# longest lock, so guessing on after it keeps locking for a day each time.
STREAK_TTL_SECONDS = 48 * 60 * 60
STREAK_FIRST_WAIT_SECONDS = 15 * 60
STREAK_MAX_WAIT_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Lockout:
    """A lock that a failure just started: how long it runs and the failures in a row behind it (``0``
    without a streak)."""

    wait: int
    streak: int


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

    def record_failure(
        self, scope: str, hashed_email: str, *, streak_limit: int | None = None
    ) -> Lockout | None:
        """Count one failure; the :class:`Lockout` it started, if any. Five in a minute lock for 1, 2, 4, 8,
        then 15 minutes; with ``streak_limit`` every ``streak_limit``-th failure in a row (no success between,
        none more than :data:`STREAK_TTL_SECONDS` apart) also locks, for 15 minutes, 1, 4, 16, then 24
        hours."""
        waits: list[int] = []
        count = _increment(f"auth:fail:{scope}:{hashed_email}", 60)
        if count == 5:
            locks = _increment(f"auth:lockn:{scope}:{hashed_email}", 24 * 60 * 60)
            waits.append(min(60 * 2 ** min(locks - 1, 4), 900))
        streak = 0
        if streak_limit:
            streak = _increment(f"auth:streak:{scope}:{hashed_email}", STREAK_TTL_SECONDS, sliding=True)
            if streak % streak_limit == 0:
                rank = min(streak // streak_limit - 1, 4)
                waits.append(min(STREAK_FIRST_WAIT_SECONDS * 4**rank, STREAK_MAX_WAIT_SECONDS))
        if not waits:
            return None
        wait = max(waits)
        key = f"auth:lock:{scope}:{hashed_email}"
        until = timeutils.now().timestamp() + wait
        running = cache.get(key)
        if running is not None and float(running) > until:  # never shorten a longer lock
            return Lockout(math.ceil(float(running) - timeutils.now().timestamp()), streak)
        cache.set(key, until, timeout=wait)
        return Lockout(wait, streak)

    def reset(self, scope: str, hashed_email: str) -> None:
        """A success: the minute's failures and attempts and the failure streak start again."""
        cache.delete_many(
            [
                f"auth:fail:{scope}:{hashed_email}",
                f"auth:attempt:{scope}:{hashed_email}",
                f"auth:streak:{scope}:{hashed_email}",
            ]
        )

    def unlock(self, scope: str, hashed_email: str) -> None:
        """End a running lockout too (a manager reset the password: the guessed one is gone). The escalation
        counter ``auth:lockn`` stays, so guessing on afterwards still locks for longer each time."""
        self.reset(scope, hashed_email)
        cache.delete(f"auth:lock:{scope}:{hashed_email}")


class IpThrottled(Throttled):
    """The client address used up its failure budget (``IpFailureBudget``), not the account its ladder."""


def client_bucket(request: HttpRequest) -> str:
    """The budget key of the request's client: its address as the console's network gate reads it
    (``peer_ip``: the edge's ``X-Forwarded-For`` hop), an IPv6 address by its /64 (one host holds a whole
    /64)."""
    raw = peer_ip(request)
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class IpFailureBudget:
    """At most ``OPS_IP_FAILURE_LIMIT`` failed attempts of one ``step`` (``password``, ``code``) per client
    address per ``OPS_IP_FAILURE_WINDOW_SECONDS`` (module doc)."""

    def __init__(self, step: str) -> None:
        self.step = step

    def _count_key(self, bucket: str) -> str:
        return f"auth:ipfail:{self.step}:{bucket}"

    def _lock_key(self, bucket: str) -> str:
        return f"auth:iplock:{self.step}:{bucket}"

    def reserve(self, bucket: str) -> None:
        """Count one attempt against the budget (raise :class:`IpThrottled` while it is used up)."""
        lock_key = self._lock_key(bucket)
        expiry = cache.get(lock_key)
        now = timeutils.now().timestamp()
        if expiry is not None:
            wait = math.ceil(float(expiry) - now)
            if wait > 0:
                raise IpThrottled(wait=wait)
            cache.delete(lock_key)
        window = int(settings.OPS_IP_FAILURE_WINDOW_SECONDS)
        if _increment(self._count_key(bucket), window) > int(settings.OPS_IP_FAILURE_LIMIT):
            cache.set(lock_key, now + window, timeout=window)
            raise IpThrottled(wait=window)

    def refund(self, bucket: str) -> None:
        """Give a reserved attempt back: it succeeded."""
        try:
            cache.decr(self._count_key(bucket))
        except ValueError:  # the window ended meanwhile: nothing to give back
            pass


def record_ip_locked(request: HttpRequest, step: str, wait: int, email: str = "") -> None:
    """Audit and alert an address over its budget (the address is in the audit row, never in the alert)."""
    events.record("console.ip_locked", request, email=email, step=step, wait=wait)
    logger.warning("console.ip_locked step=%s wait=%s", step, wait)
    alerts.alert("console.ip_locked", {"step": step, "wait": wait}, scope=step)


# Unfold's admin login form: a ``django.contrib.admin.forms.AdminAuthenticationForm`` with styled widgets.
if TYPE_CHECKING:
    _AdminAuthenticationForm = AdminAuthenticationForm
else:
    _AdminAuthenticationForm = UnfoldAuthenticationForm


PASSWORD_CHANGE_REQUIRED_CODE = "password_change_required"  # noqa: S105 - an error code


def console_password_change_required() -> str:
    """The console login refusal of an account with a pending forced password change (its language)."""
    return gettext("Avval admin panelda o‘z parolingizni o‘rnating, so‘ng konsolga qayta kiring.")


class GuardedAdminAuthenticationForm(_AdminAuthenticationForm):
    def clean(self) -> dict[str, Any]:
        username = str(self.cleaned_data.get("username", "")).strip().lower()
        self.cleaned_data["username"] = username
        budget = IpFailureBudget("password")
        bucket = client_bucket(self.request) if self.request is not None else ""
        if bucket:
            budget.reserve(bucket)
        guard = LoginGuard()
        hashed = email_hash(username)
        guard.check("django_admin_login", hashed)
        try:
            result = super().clean()
        except ValidationError:
            guard.record_failure("django_admin_login", hashed)
            raise
        guard.reset("django_admin_login", hashed)
        if bucket:
            budget.refund(bucket)
        user = self.get_user()
        if user is not None and getattr(user, "must_change_password", False):
            # The right password, but someone else chose it (feat/admin-ux): the person sets their own in the
            # admin panel first. Not a failed guess (the lockout and the address budget stay as they are).
            raise ValidationError(console_password_change_required(), code=PASSWORD_CHANGE_REQUIRED_CODE)
        return result


# Wired once at import time (config.urls imports this module); the view never mutates the site.
admin.site.login_form = GuardedAdminAuthenticationForm

LOGIN_TEMPLATE = "accounts/console/login.html"


def lockout_message(exc: Throttled) -> tuple[str, int]:
    """The lockout notice in the console's language, and the wait in seconds."""
    wait = int(math.ceil(float(getattr(exc, "wait", None) or 0)))
    notice = ngettext(
        "Juda ko‘p urinish. %(wait)s soniyadan so‘ng qayta urinib ko‘ring.",
        "Juda ko‘p urinish. %(wait)s soniyadan so‘ng qayta urinib ko‘ring.",
        wait,
    ) % {"wait": wait}
    return notice, wait


class ConsolePasswordView(LoginView):
    """The console's first step. A correct password logs nobody in while the second factor is required
    (``OPS_REQUIRE_2FA``): it records a pending sign-in and sends the browser to the code step, or to the
    enrolment step when the user has no confirmed TOTP device yet (``glowderma.accounts.console``)."""

    def form_valid(self, form: Any) -> HttpResponse:
        user = form.get_user()
        next_url = state.safe_next(self.request, self.get_redirect_url() or None)
        if not settings.OPS_REQUIRE_2FA:
            state.complete_login(self.request, user, None)
            events.record("console.login_succeeded", self.request, user, method="password")
            return HttpResponseRedirect(next_url)
        state.start_pending(self.request, user, next_url)
        events.record("console.password_verified", self.request, user)
        step = (
            "ops_2fa_verify"
            if twofactor.has_second_factor(user, scope=twofactor.CONSOLE)
            else "ops_2fa_enrol"
        )
        return HttpResponseRedirect(reverse(step))

    def form_invalid(self, form: Any) -> HttpResponse:
        email = str(form.data.get("username", "")).strip().lower()[:254]
        pending = form.has_error(NON_FIELD_ERRORS, PASSWORD_CHANGE_REQUIRED_CODE)
        events.record(
            "console.login_failed",
            self.request,
            email=email,
            reason=PASSWORD_CHANGE_REQUIRED_CODE if pending else "",
        )
        return super().form_invalid(form)


def _login_context(request: HttpRequest, extra_context: dict[str, Any] | None) -> dict[str, Any]:
    """What ``AdminSite.login`` hands the template, so Unfold's login page renders unchanged."""
    context: dict[str, Any] = {
        **admin.site.each_context(request),
        "title": gettext("Kirish"),
        "subtitle": None,
        "app_path": request.get_full_path(),
        "username": "",
    }
    if REDIRECT_FIELD_NAME not in request.GET and REDIRECT_FIELD_NAME not in request.POST:
        context[REDIRECT_FIELD_NAME] = reverse("admin:index", current_app=admin.site.name)
    context.update(extra_context or {})
    return context


def guarded_admin_login(
    request: HttpRequest, extra_context: dict[str, Any] | None = None
) -> HttpResponseBase:
    request.current_app = admin.site.name
    if request.method == "GET" and admin.site.has_permission(request):
        return HttpResponseRedirect(reverse("admin:index", current_app=admin.site.name))
    context = _login_context(request, extra_context)
    try:
        return ConsolePasswordView.as_view(
            extra_context=context,
            authentication_form=admin.site.login_form,
            template_name=LOGIN_TEMPLATE,
        )(request)
    except Throttled as exc:
        message, wait = lockout_message(exc)
        email = str(request.POST.get("username", "")).strip().lower()[:254]
        if isinstance(exc, IpThrottled):
            record_ip_locked(request, "password", wait, email=email)
        else:
            events.record("console.login_locked", request, email=email, wait=wait)
        form = GuardedAdminAuthenticationForm(request, initial={"username": email})
        response = TemplateResponse(
            request, LOGIN_TEMPLATE, {**context, "form": form, "lockout_message": message}, status=429
        )
        response["Retry-After"] = str(wait)
        add_never_cache_headers(response)
        return response
