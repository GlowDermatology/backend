"""«Savdo va katalog» — sales by day and channel, the order funnel, top products, catalog health and the SEO
backlog, over a 7 / 30 / 90-day window (``?days=``).

Revenue follows the panel dashboard's rule (payments confirmed in the day net of their succeeded refunds,
late captures excluded) split by the order's channel; "POS" is the in-store QR purchases (their gross
amount, loyalty ``Purchase``). Statements: revenue 2 (window + previous), paid orders 2, POS 1, funnel 1,
top products 1, stock 3; the SEO backlog runs the catalog checklist in Python over the active catalog
(2 statements) and is cached for :data:`SEO_CACHE_SECONDS` on its own.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from glowderma.catalog import selectors as catalog_selectors
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.labels import ORDER_STATUS
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.payments import selectors as payment_selectors
from glowderma.reporting.dashboards import (
    CRITICAL,
    LINK_IDS,
    NEUTRAL,
    OK,
    WARN,
    by_count,
    cached,
    change,
    changelist,
    generated,
    iso,
    num,
    percent,
    som,
    som_short,
    state,
)

WINDOWS = (7, 30, 90)
DEFAULT_WINDOW = 30
TOP_PRODUCTS = 10
STALE_STOCK_AFTER = catalog_selectors.STALE_STOCK_AFTER
SEO_CACHE_SECONDS = 600
POS = "pos"
# Channel order of the stacked chart (bottom → top) and its validated palette slots (dashboards.css).
CHANNELS: tuple[tuple[str, str], ...] = (
    (enums.OrderChannel.WEB, "Veb-sayt"),
    (enums.OrderChannel.IOS, "iOS"),
    (enums.OrderChannel.ANDROID, "Android"),
    (POS, "Do‘kon (POS)"),
)
SEO_CODE_LABELS: dict[str, str] = {
    enums.SeoIssueCode.NAME_MISSING: "O‘zbekcha nom yo‘q",
    enums.SeoIssueCode.NAME_TRANSLATION_MISSING: "Nom tarjimasi yo‘q",
    enums.SeoIssueCode.DESCRIPTION_MISSING: "Tavsif yo‘q",
    enums.SeoIssueCode.DESCRIPTION_SHORT: "Tavsif juda qisqa",
    enums.SeoIssueCode.IMAGES_MISSING: "Rasm yo‘q",
    enums.SeoIssueCode.ALT_TEXT_FALLBACK: "Alt matni nomdan olinadi",
    enums.SeoIssueCode.CATEGORY_MISSING: "Kategoriya tanlanmagan",
    enums.SeoIssueCode.SEO_TITLE_SHORT: "SEO sarlavha qisqa",
    enums.SeoIssueCode.SEO_TITLE_LONG: "SEO sarlavha uzun",
    enums.SeoIssueCode.SEO_DESCRIPTION_SHORT: "SEO tavsif qisqa",
    enums.SeoIssueCode.SEO_DESCRIPTION_LONG: "SEO tavsif uzun",
    enums.SeoIssueCode.IMAGE_MISSING: "Kategoriya rasmi yo‘q",
    enums.SeoIssueCode.NO_ACTIVE_PRODUCTS: "Faol mahsuloti yo‘q",
}
# A code whose severity depends on the language: say which texts the row counts.
SEO_SEVERITY_HINTS: dict[tuple[str, str], str] = {
    (enums.SeoIssueCode.DESCRIPTION_MISSING, enums.SeoSeverity.ERROR): " (o‘zbekcha)",
    (enums.SeoIssueCode.DESCRIPTION_MISSING, enums.SeoSeverity.WARNING): " (ruscha / inglizcha)",
}
STATUS_ORDER = (
    enums.OrderStatus.AWAITING_PAYMENT,
    enums.OrderStatus.PROCESSING,
    enums.OrderStatus.HANDED_TO_COURIER,
    enums.OrderStatus.DELIVERED,
    enums.OrderStatus.CANCELLED,
)


def window_days(raw: str | None) -> int:
    """The ``?days=`` window: one of :data:`WINDOWS`, else :data:`DEFAULT_WINDOW`."""
    try:
        days = int(raw or DEFAULT_WINDOW)
    except ValueError:
        return DEFAULT_WINDOW
    return days if days in WINDOWS else DEFAULT_WINDOW


def _days(first: date, count: int) -> list[date]:
    return [first + timedelta(days=offset) for offset in range(count)]


def seo_backlog() -> dict[str, Any]:
    """The catalog SEO backlog as plain data (cached apart: it runs the checklist over the whole catalog)."""
    backlog = catalog_selectors.seo_backlog(limit=LINK_IDS)
    data = asdict(backlog)
    data["issues"] = [asdict(issue) for issue in backlog.issues]
    return data


def _chart(days: list[date], series: dict[str, list[int]]) -> dict[str, str]:
    datasets = [
        {
            "label": label,
            "data": series[key],
            "backgroundColor": f"var(--gd-series-{index})",
            "gdColor": f"--gd-series-{index}",
            "borderColor": "var(--gd-surface)",
            "borderWidth": {"top": 2, "right": 0, "bottom": 0, "left": 0},
            "borderSkipped": False,
            "borderRadius": 2,
            "maxBarThickness": 26,
            "stack": "revenue",
        }
        for index, (key, label) in enumerate(CHANNELS, start=1)
    ]
    options = {
        "animation": False,
        "responsive": True,
        "maintainAspectRatio": False,
        "interaction": {"mode": "index", "intersect": False},
        "plugins": {
            "legend": {
                "display": True,
                "position": "top",
                "align": "end",
                "labels": {
                    "boxWidth": 8,
                    "boxHeight": 8,
                    "usePointStyle": True,
                    "pointStyle": "circle",
                    "color": "#9ca3af",
                },
            },
            "tooltip": {"enabled": True},
        },
        "scales": {
            "x": {
                "stacked": True,
                "grid": {"display": False},
                "border": {"display": False},
                "ticks": {"color": "#9ca3af", "maxTicksLimit": 10, "autoSkip": True, "maxRotation": 0},
            },
            "y": {
                "stacked": True,
                "beginAtZero": True,
                "border": {"display": False},
                "grid": {"display": True},
                "ticks": {"color": "#9ca3af", "maxTicksLimit": 5},
            },
        },
    }
    labels = [day.strftime("%d.%m") for day in days]
    return {
        "data": json.dumps({"labels": labels, "datasets": datasets}, ensure_ascii=False),
        "options": json.dumps(options),
    }


def build(now: datetime, days: int) -> dict[str, Any]:
    today = timeutils.local_date(now)
    first = today - timedelta(days=days - 1)
    start, end = timeutils.day_bounds(first)[0], timeutils.day_bounds(today)[1]
    previous_start = timeutils.day_bounds(first - timedelta(days=days))[0]
    revenue = payment_selectors.revenue_by_day_and_channel(start, end)
    orders = commerce_selectors.paid_orders_by_day_and_channel(start, end)
    previous_revenue = sum(payment_selectors.revenue_by_day_and_channel(previous_start, start).values())
    previous_orders = sum(commerce_selectors.paid_orders_by_day_and_channel(previous_start, start).values())
    pos = loyalty_selectors.store_purchases_by_day(start, end)
    funnel = commerce_selectors.status_breakdown(start, end)
    top = commerce_selectors.top_products(start, end, limit=TOP_PRODUCTS)
    stock = catalog_selectors.stock_health(now - STALE_STOCK_AFTER, limit=LINK_IDS)
    seo = cached("seo", seo_backlog, seconds=SEO_CACHE_SECONDS)

    calendar = _days(first, days)
    series: dict[str, list[int]] = {key: [] for key, _label in CHANNELS}
    for day in calendar:
        for key, _label in CHANNELS:
            value = pos.get(day, (0, 0))[1] if key == POS else revenue.get((day, key), 0)
            series[key].append(value)
    channel_rows = []
    online_revenue = sum(revenue.values())
    online_orders = sum(orders.values())
    pos_count = sum(count for count, _amount in pos.values())
    pos_amount = sum(amount for _count, amount in pos.values())
    grand = online_revenue + pos_amount
    for index, (key, label) in enumerate(CHANNELS, start=1):
        if key == POS:
            count, amount = pos_count, pos_amount
            href = changelist(
                "loyalty_purchase",
                source__exact="store_qr",
                reversed__exact="0",
                at__gte=iso(start),
                at__lt=iso(end),
            )
        else:
            count = sum(n for (_day, channel), n in orders.items() if channel == key)
            amount = sum(value for (_day, channel), value in revenue.items() if channel == key)
            href = changelist(
                "commerce_order", channel__exact=key, paid_at__gte=iso(start), paid_at__lt=iso(end)
            )
        channel_rows.append(
            {
                "key": key,
                "label": label,
                "slot": index,
                "orders": num(count),
                "revenue": som(amount),
                "share": percent(amount, grand),
                "aov": som(amount // count) if count else "—",
                "href": href,
                "raw": {"orders": count, "revenue": amount},
            }
        )
    created_total = sum(funnel.values())
    funnel_rows = [
        {
            "status": status,
            "label": ORDER_STATUS[status],
            "count": num(funnel[status]),
            "raw": funnel[status],
            "percent": percent(funnel[status], created_total),
            "href": changelist(
                "commerce_order", status__exact=status, created_at__gte=iso(start), created_at__lt=iso(end)
            ),
        }
        for status in STATUS_ORDER
    ]
    aov = online_revenue // online_orders if online_orders else 0
    previous_aov = previous_revenue // previous_orders if previous_orders else 0

    def delta(current: int, before: int) -> dict[str, Any] | None:
        if not before:
            return None
        change_pct = round(100 * (current - before) / before)
        return {"text": f"{'+' if change_pct >= 0 else '−'}{abs(change_pct)}%", "up": change_pct >= 0}

    orders_window = changelist("commerce_order", paid_at__gte=iso(start), paid_at__lt=iso(end))
    tiles = [
        {
            "title": "Onlayn tushum",
            "icon": "payments",
            "value": som_short(online_revenue),
            "unit": "",
            "hint": som(online_revenue),
            "delta": delta(online_revenue, previous_revenue),
            "href": changelist(
                "payments_payment",
                status__in="paid,refunded",
                confirmed_at__gte=iso(start),
                confirmed_at__lt=iso(end),
            ),
            "state": state(NEUTRAL),
        },
        {
            "title": "To‘langan buyurtmalar",
            "icon": "shopping_bag",
            "value": num(online_orders),
            "unit": "ta",
            "hint": f"Oldingi {days} kun: {num(previous_orders)}",
            "delta": delta(online_orders, previous_orders),
            "href": orders_window,
            "state": state(NEUTRAL),
        },
        {
            "title": "O‘rtacha chek",
            "icon": "receipt_long",
            "value": som_short(aov) if aov else "—",
            "unit": "",
            "hint": som(aov) if aov else "To‘langan buyurtma yo‘q",
            "delta": delta(aov, previous_aov),
            "href": orders_window,
            "state": state(NEUTRAL),
        },
        {
            "title": "Do‘kon xaridlari (POS)",
            "icon": "storefront",
            "value": num(pos_count),
            "unit": "ta",
            "hint": som(pos_amount),
            "delta": None,
            "href": channel_rows[-1]["href"],
            "state": state(NEUTRAL),
        },
    ]
    seo_rows = [
        {
            "kind": "Mahsulot" if issue["kind"] == "product" else "Kategoriya",
            "label": SEO_CODE_LABELS.get(issue["code"], issue["code"])
            + SEO_SEVERITY_HINTS.get((issue["code"], issue["severity"]), ""),
            "code": issue["code"],
            "count": num(issue["count"]),
            "raw": issue["count"],
            # the list's «SEO muammosi» filter: every row of the issue (an id list stops at LINK_IDS)
            "href": changelist(
                "catalog_product" if issue["kind"] == "product" else "catalog_category",
                seo=f"{issue['code']}.{issue['severity']}",
            ),
            "state": state(
                CRITICAL if issue["severity"] == "error" else WARN,
                "Xato" if issue["severity"] == "error" else "Ogohlantirish",
            ),
        }
        for issue in seo["issues"]
    ]
    billz_live = integration_modes.provider_mode("billz") == enums.IntegrationMode.LIVE
    stale_tile: dict[str, Any] = {
        "title": "Eskirgan qoldiq",
        "icon": "update",
        "value": num(stock.stale.count),
        "unit": "ta mahsulot",
        "hint": "Billz qoldig‘i 24 soatdan beri yangilanmagan (faol mahsulotlar)",
        # the product list's «Qoldiq» filter selects the same rows (an id list would stop at LINK_IDS)
        "href": changelist("catalog_product", status__exact=enums.ProductStatus.ACTIVE, stock="stale"),
        "state": state(by_count(stock.stale.count)),
    }
    if not billz_live:
        # Billz is the fake adapter: nothing refreshes the quantities, so «stale» is no alarm (as on Ops).
        stale_tile.update(
            {
                "state": state(NEUTRAL, "Sinov rejimi"),
                "muted": True,
                "hint": "Billz sinov rejimida: qoldiq yangilanmaydi",
            }
        )
    catalog_tiles = [
        {
            "title": "Tugagan mahsulotlar",
            "icon": "remove_shopping_cart",
            "value": num(stock.out_of_stock.count),
            "unit": "ta mahsulot",
            "hint": "Faol, lekin qoldig‘i 0 yoki Billz’da yozuvi yo‘q",
            "href": changelist("catalog_product", status__exact=enums.ProductStatus.ACTIVE, stock="out"),
            "state": state(by_count(stock.out_of_stock.count)),
        },
        stale_tile,
        {
            "title": "Billz’dan yangi",
            "icon": "new_releases",
            "value": num(stock.new_from_billz),
            "unit": "ko‘rib chiqilmagan",
            "hint": "Import qilingan, hali tekshirilmagan",
            "href": changelist("catalog_product", is_new_from_billz__exact="1"),
            "state": state(WARN if stock.new_from_billz else OK),
        },
        {
            "title": "SEO xatolari",
            "icon": "travel_explore",
            "value": num(seo["products_with_errors"]),
            "unit": "mahsulot",
            "hint": f"{num(seo['products_with_warnings'])} ogohlantirishli · "
            f"{num(seo['categories_with_issues'])} kategoriya",
            "href": "#seo",
            "state": state(by_count(seo["products_with_errors"])),
        },
    ]
    return {
        "generated": generated(now),
        "days": days,
        "windows": [
            {"days": option, "title": f"{option} kun", "link": f"?days={option}", "active": option == days}
            for option in WINDOWS
        ],
        "period": f"{first.strftime('%d.%m.%Y')} – {today.strftime('%d.%m.%Y')}",
        "tiles": tiles,
        "chart": _chart(calendar, series),
        "has_sales": grand > 0,
        "channels": channel_rows,
        "funnel": funnel_rows,
        "funnel_total": num(created_total),
        "top": [
            {
                "name": product.name,
                "quantity": num(product.quantity),
                "orders": num(product.orders),
                "revenue": som(product.revenue),
                "href": change("catalog_product", product.product_id),
            }
            for product in top
        ],
        "catalog_tiles": catalog_tiles,
        "seo": seo_rows,
        "seo_checked": {
            "products": num(seo["products_checked"]),
            "categories": num(seo["categories_checked"]),
        },
        "raw": {
            "online_revenue": online_revenue,
            "online_orders": online_orders,
            "pos_count": pos_count,
            "pos_amount": pos_amount,
            "aov": aov,
        },
    }


__all__ = ["DEFAULT_WINDOW", "WINDOWS", "build", "seo_backlog", "window_days"]
