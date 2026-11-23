"""``CSRF_FAILURE_VIEW``: the console's own 403 page in the console's language."""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext

from glowderma.accounts import console_sessions
from glowderma.accounts.console import notices, paths
from glowderma.accounts.console.views import rendering
from glowderma.core.network import CONSOLE_PREFIX


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """``CSRF_FAILURE_VIEW``: a console page in the console's language (403) for a console path; Django's own
    page elsewhere (no other part of the project uses session forms)."""
    if not request.path_info.startswith(CONSOLE_PREFIX):
        from django.views.csrf import csrf_failure as django_csrf_failure

        return django_csrf_failure(request, reason)
    target = paths.LOGIN_PATH if not admin.site.has_permission(request) else console_sessions.CONSOLE_INDEX
    context = rendering.console_context(
        request, gettext("So‘rov qabul qilinmadi"), csrf_message=notices.CSRF_FAILED, next_url=target
    )
    return rendering.render_step(request, "accounts/console/csrf_failure.html", context, status=403)


__all__ = [
    "csrf_failure",
]
