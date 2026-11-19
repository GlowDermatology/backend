"""Who may open the AI assistant's console pages: active staff with ``is_superuser`` (2FA is enforced by the
console itself, ``core.admin_site``). Used by every view (``has_permission``) and by the sidebar entries
(``"permission": "glowderma.assistant.console.access.is_superuser"``)."""

from __future__ import annotations

from django.http import HttpRequest

from glowderma.core.admin import is_active_staff


def is_superuser(request: HttpRequest) -> bool:
    return is_active_staff(request) and bool(getattr(request.user, "is_superuser", False))
