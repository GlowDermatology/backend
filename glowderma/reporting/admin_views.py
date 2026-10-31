"""The ops console's dashboards as Unfold pages (config/settings/ops_admin.py).

* «Ops holati» is the console index: ``UNFOLD["DASHBOARD_CALLBACK"]`` = :func:`dashboard_callback` adds the
  data to the index context and ``OpsAdminSite.index_template`` (glowderma.core.admin_site) names
  ``reporting/dashboards/ops.html``.
* «Pul solishtirish», «Savdo va katalog», «Mijozlar va kirish» are ``UNFOLD["SITE_VIEWS"]`` pages:
  Unfold mounts each under the admin site through ``admin_site.admin_view`` (so the console's login — and any
  later second factor that wraps ``admin_view`` — guards them) and :class:`UnfoldSiteViewMixin` gives them the
  console layout (``admin_site.each_context``).

Every dashboard is built by :mod:`glowderma.reporting.dashboards` and cached for a minute; ``?refresh=1``
rebuilds it. Only active staff (``core.admin.is_active_staff``, the read-only admin's grant) see them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.http import HttpRequest
from django.urls import reverse
from django.views.generic import TemplateView
from unfold.views import UnfoldSiteViewMixin

from glowderma.core import timeutils
from glowderma.core.admin import is_active_staff
from glowderma.reporting import dashboards
from glowderma.reporting.dashboards import customers, money, ops, sales

OPS_TEMPLATE = "reporting/dashboards/ops.html"


def _refresh(request: HttpRequest) -> bool:
    return request.GET.get("refresh") == "1"


def _load(name: str, build: Callable[[], dict[str, Any]], request: HttpRequest) -> dict[str, Any]:
    return dashboards.cached(name, build, refresh=_refresh(request))


def _links(request: HttpRequest, **params: str) -> dict[str, str]:
    """The page's own URL and its «Yangilash» link (``refresh=1``, other parameters kept)."""
    query = request.GET.copy()
    query.pop("refresh", None)
    for key, value in params.items():
        query[key] = value
    base = request.path
    plain = f"{base}?{query.urlencode()}" if query else base
    query["refresh"] = "1"
    return {"self": plain, "refresh": f"{base}?{query.urlencode()}"}


def dashboard_callback(request: HttpRequest, context: dict[str, Any]) -> dict[str, Any]:
    """``UNFOLD["DASHBOARD_CALLBACK"]``: the «Ops holati» data on the console index."""
    if not is_active_staff(request):  # the index is only reachable by staff; never build for anyone else
        return context
    data = _load("ops", lambda: ops.build(timeutils.now()), request)
    context.update(
        {
            "title": "Ops holati",
            "dashboard": data,
            "dashboard_links": _links(request),
            "dashboard_nav": nav(request),
        }
    )
    return context


def nav(request: HttpRequest) -> list[dict[str, Any]]:
    """The four dashboards as a tab strip (Unfold ``navigation`` component)."""
    items = [
        ("Ops holati", "monitor_heart", reverse("admin:index")),
        ("Pul solishtirish", "account_balance", reverse("admin:reporting_money")),
        ("Savdo va katalog", "query_stats", reverse("admin:reporting_sales")),
        ("Mijozlar va kirish", "diversity_3", reverse("admin:reporting_customers")),
    ]
    return [
        {"title": title, "icon": icon, "link": link, "active": request.path == link}
        for title, icon, link in items
    ]


class DashboardView(UnfoldSiteViewMixin, TemplateView):  # type: ignore[misc]  # Unfold is untyped
    """A dashboard page in the console layout; subclasses name the template, the cache key and the build."""

    title = ""
    cache_name = ""

    def has_permission(self) -> bool:
        return is_active_staff(self.request)

    def build(self) -> dict[str, Any]:
        raise NotImplementedError

    def cache_key(self) -> str:
        return self.cache_name

    def link_params(self) -> dict[str, str]:
        return {}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "dashboard": _load(self.cache_key(), self.build, self.request),
                "dashboard_links": _links(self.request, **self.link_params()),
                "dashboard_nav": nav(self.request),
            }
        )
        return context


class MoneyDashboardView(DashboardView):
    title = "Pul solishtirish"
    template_name = "reporting/dashboards/money.html"
    cache_name = "money"

    def build(self) -> dict[str, Any]:
        return money.build(timeutils.now())


class SalesDashboardView(DashboardView):
    title = "Savdo va katalog"
    template_name = "reporting/dashboards/sales.html"
    cache_name = "sales"

    def days(self) -> int:
        return sales.window_days(self.request.GET.get("days"))

    def cache_key(self) -> str:
        return f"{self.cache_name}:{self.days()}"

    def link_params(self) -> dict[str, str]:
        return {"days": str(self.days())}

    def build(self) -> dict[str, Any]:
        return sales.build(timeutils.now(), self.days())


class CustomersDashboardView(DashboardView):
    title = "Mijozlar, kirish va sodiqlik"
    template_name = "reporting/dashboards/customers.html"
    cache_name = "customers"

    def build(self) -> dict[str, Any]:
        return customers.build(timeutils.now())


__all__ = [
    "OPS_TEMPLATE",
    "CustomersDashboardView",
    "MoneyDashboardView",
    "SalesDashboardView",
    "dashboard_callback",
    "nav",
]
