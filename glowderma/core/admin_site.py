"""The Glow Derma ops console: the project's default admin site (``admin.site``), styled by Unfold.

Seams for the later console work:

* ``UNFOLD`` (``config/settings/ops_admin.py``) holds the theme, the sidebar and the callbacks below;
  dashboards plug in through ``UNFOLD["DASHBOARD_CALLBACK"]`` and ``UNFOLD["SITE_VIEWS"]`` (custom
  pages), never by subclassing this site again.
* The login form is wired by ``glowderma.accounts.api.guard`` (the lockout lives above ``core``); the login,
  2FA and logout views live in ``glowderma.accounts.console`` and are mounted ahead of the site's URLs.
* The second factor is enforced here, without importing ``accounts``: every console view (``admin_view``,
  hence every model page, custom page and the index) requires ``request.user.is_verified()``, which
  django-otp's ``OTPMiddleware`` installs from the session's verified device (``OPS_REQUIRE_2FA``).
* No password change here: the stock ``password_change/`` and ``password_change/done/`` pages answer 404 (a
  console session could otherwise change the password with no lockout, no audit and without ending the
  user's Next admin panel sessions). Staff change their password in the Next admin panel
  (``auth/change-password/``: lockout, session revoke, audit); the user menu links to the user's own staff
  record instead (``UNFOLD["ACCOUNT"]``, :func:`account_links`), so Unfold never renders a dead
  "Change password" link.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import Http404, HttpRequest
from django.urls import reverse
from unfold.sites import UnfoldAdminSite

if TYPE_CHECKING:
    from django.contrib.admin import AdminSite as _BaseSite
else:
    _BaseSite = UnfoldAdminSite

SITE_NAME = "Glow Derma Ops"

# Label shown in the header badge and its Unfold colour variant.
ENVIRONMENT_VARIANTS = {"PRODUCTION": "danger", "STAGING": "warning", "LOCAL": "info", "TEST": "info"}


class OpsAdminSite(_BaseSite):
    site_header = SITE_NAME
    site_title = SITE_NAME
    index_title = "Boshqaruv"
    # The index is the «Ops holati» dashboard (glowderma.reporting; ``UNFOLD["DASHBOARD_CALLBACK"]`` fills its
    # context). Named, not imported, so ``core`` stays below ``reporting``: Unfold's own ``admin/index.html``
    # comes first in the template search, hence the explicit name.
    index_template = "reporting/dashboards/ops.html"
    # No public "view site" link: the console has no storefront of its own.
    site_url = None

    def has_permission(self, request: HttpRequest) -> bool:
        """An active staff user whose session passed the second factor (fails closed without
        ``OTPMiddleware``: no ``is_verified`` means unverified)."""
        if not super().has_permission(request):
            return False
        if not settings.OPS_REQUIRE_2FA:
            return True
        is_verified = getattr(request.user, "is_verified", None)
        return callable(is_verified) and bool(is_verified())

    def password_change(self, request: HttpRequest, extra_context: dict[str, Any] | None = None) -> Any:
        """Not a console page: passwords change in the Next admin panel (module doc)."""
        raise Http404

    def password_change_done(self, request: HttpRequest, extra_context: dict[str, Any] | None = None) -> Any:
        raise Http404


ACCOUNT_LINK_TITLE = "Mening hisobim"


def account_links(request: HttpRequest) -> list[dict[str, str]]:
    """``UNFOLD["ACCOUNT"]["navigation"]``: the user menu's link to the signed-in user's own (read-only) staff
    record. A non-empty list is what keeps Unfold from rendering its stock "Change password" link."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.pk is None:
        return []
    opts = get_user_model()._meta
    link = reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[user.pk])
    return [{"title": ACCOUNT_LINK_TITLE, "link": link}]


def environment_callback(request: HttpRequest) -> list[str]:
    """``UNFOLD["ENVIRONMENT"]``: the header badge ``[label, variant]`` from ``OPS_ENVIRONMENT_LABEL``."""
    label = str(getattr(settings, "OPS_ENVIRONMENT_LABEL", "LOCAL")).upper()
    return [label, ENVIRONMENT_VARIANTS.get(label, "warning")]


def environment_title_prefix(request: HttpRequest) -> str:
    """``UNFOLD["ENVIRONMENT_TITLE_PREFIX"]``: ``[STAGING]`` in the browser tab outside production."""
    label = str(getattr(settings, "OPS_ENVIRONMENT_LABEL", "LOCAL")).upper()
    return "" if label == "PRODUCTION" else f"[{label}]"
