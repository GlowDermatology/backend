"""The Unfold configuration of the ops console (``/django-admin/``, ``glowderma.core.admin_site``).

Imported by ``config.settings.base``. Seams for the later console work:

* dashboards — ``UNFOLD["DASHBOARD_CALLBACK"]`` (the index page) and ``UNFOLD["SITE_VIEWS"]`` (custom pages),
  linked from the first sidebar group (``DASHBOARD_GROUP``);
* live badges — ``glowderma.reporting.admin_nav`` (cached counts, never a heavy query on a page load);
* the environment badge — ``OPS_ENVIRONMENT_LABEL`` (``LOCAL`` here, ``PRODUCTION``/``STAGING`` from the
  production settings).

Brand colours are the storefront design tokens (``frontend/src/app/globals.css``: gold ``#c9a24b``,
gold ink ``#9a7424``, gold soft ``#f6ebd0``, ink ``#2a2418``, surface ``#fbf8f2``, nav ``#2b2620``)
converted to OKLCH and spread over Unfold's 50–950 scales (hue ≈ 84°, the gold family; the neutrals are
the same warm taupe).
"""

from typing import Any

from django.templatetags.static import static
from django.urls import reverse_lazy

STAFF_ONLY = "glowderma.core.admin.is_active_staff"
NAV_BADGES = "glowderma.reporting.admin_nav"


def _changelist(app_label: str, model_name: str) -> Any:
    return reverse_lazy(f"admin:{app_label}_{model_name}_changelist")


def _item(title: str, icon: str, app_label: str, model_name: str, badge: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "icon": icon,
        "link": _changelist(app_label, model_name),
        "permission": STAFF_ONLY,
    }
    if badge is not None:
        item["badge"] = f"{NAV_BADGES}.{badge}"
        item["badge_variant"] = "danger"
    return item


DASHBOARD_VIEWS = "glowderma.reporting.admin_views"


def _dashboard(title: str, icon: str, url_name: str) -> dict[str, Any]:
    return {"title": title, "icon": icon, "link": reverse_lazy(url_name), "permission": STAFF_ONLY}


# The four dashboards of the console (glowderma.reporting.dashboards): the index is «Ops holati».
DASHBOARD_GROUP: dict[str, Any] = {
    "title": "Boshqaruv",
    "separator": False,
    "collapsible": False,
    "items": [
        _dashboard("Ops holati", "monitor_heart", "admin:index"),
        _dashboard("Pul solishtirish", "account_balance", "admin:reporting_money"),
        _dashboard("Savdo va katalog", "query_stats", "admin:reporting_sales"),
        _dashboard("Mijozlar va kirish", "diversity_3", "admin:reporting_customers"),
    ],
}

SIDEBAR_NAVIGATION: list[dict[str, Any]] = [
    DASHBOARD_GROUP,
    {
        "title": "Savdo",
        "separator": True,
        "collapsible": False,
        "items": [
            _item("Buyurtmalar", "shopping_bag", "commerce", "order"),
            _item("To‘lovlar", "payments", "payments", "payment"),
            _item("Qaytarishlar", "currency_exchange", "payments", "refund", badge="refunds_needing_action"),
            _item("Yetkazib berish", "local_shipping", "delivery", "shipment"),
            _item("Savatlar", "shopping_cart", "commerce", "cart"),
            _item("Buyurtma qatorlari", "list_alt", "commerce", "orderitem"),
            _item("Buyurtma tarixi", "history", "commerce", "orderevent"),
            _item("Zaxira bandlari", "inventory", "commerce", "stockreservation"),
            _item("Savat qatorlari", "add_shopping_cart", "commerce", "cartitem"),
            _item("Yetkazish tarixi", "route", "delivery", "shipmentevent"),
        ],
    },
    {
        "title": "Katalog",
        "separator": True,
        "collapsible": True,
        "items": [
            _item("Mahsulotlar", "inventory_2", "catalog", "product"),
            _item("Kategoriyalar", "category", "catalog", "category"),
            _item("Qoldiqlar", "warehouse", "catalog", "productstock"),
            _item("Rasmlar", "image", "catalog", "productimage"),
            _item("Bannerlar", "view_carousel", "catalog", "banner"),
            _item("Sozlamalar", "tune", "site_config", "sitesettings"),
        ],
    },
    {
        "title": "Mijozlar va sodiqlik",
        "separator": True,
        "collapsible": True,
        "items": [
            _item("Mijozlar", "group", "accounts", "customer"),
            _item("Manzillar", "home_pin", "accounts", "address"),
            _item("Ijtimoiy kirishlar", "account_circle", "accounts", "customersocialidentity"),
            _item("Cashback hisoblari", "account_balance_wallet", "loyalty", "loyaltyaccount"),
            _item("Cashback daftari", "receipt_long", "loyalty", "ledgerentry"),
            _item("Cashback bandlari", "lock_clock", "loyalty", "cashbackhold"),
            _item("Xaridlar", "storefront", "loyalty", "purchase"),
            _item("Xarid qatorlari", "format_list_bulleted", "loyalty", "purchaseitem"),
            _item("Cashback siyosati", "policy", "loyalty", "cashbackpolicy"),
            _item("Cashback darajalari", "stairs", "loyalty", "cashbacktier"),
            _item("POS qurilmalar", "point_of_sale", "loyalty", "posdevice"),
        ],
    },
    {
        "title": "Klinika",
        "separator": True,
        "collapsible": True,
        "items": [
            _item("Qabullar", "event", "appointments", "appointment"),
            _item("Qabul tarixi", "event_note", "appointments", "appointmentevent"),
            _item("Mutaxassislar", "medical_services", "professionals", "professional"),
            _item("Xizmatlar", "spa", "professionals", "service"),
            _item("Mutaxassis xizmatlari", "assignment_ind", "professionals", "professionalservice"),
            _item("Ish vaqti", "schedule", "professionals", "workinghours"),
            _item("Kun ish vaqti", "calendar_month", "professionals", "workinghoursoverride"),
            _item("Band vaqtlar", "block", "professionals", "blockedperiod"),
            _item("Filiallar", "location_on", "professionals", "location"),
        ],
    },
    {
        "title": "Xabarnomalar",
        "separator": True,
        "collapsible": True,
        "items": [
            _item("Kampaniyalar", "campaign", "notifications", "campaign"),
            _item("Qabul qiluvchilar", "group_add", "notifications", "campaignrecipient"),
            _item("Inbox", "inbox", "notifications", "inboxnotification"),
            _item("Segmentlar", "groups", "notifications", "customersegment"),
            _item("Segment a’zolari", "person_add", "notifications", "segmentmember"),
            _item("Holat shablonlari", "sms", "notifications", "statustemplate"),
        ],
    },
    {
        "title": "Integratsiyalar va navbatlar",
        "separator": True,
        "collapsible": False,
        "items": [
            _item("Outbox", "outbox", "core", "outboxmessage", badge="dead_outbox"),
            _item("Webhooklar", "webhook", "integrations", "webhookinbox", badge="failed_webhooks"),
            _item("Fon vazifalari", "sync", "integrations", "job"),
            _item("Integratsiya holati", "hub", "integrations", "integrationstate"),
            _item("Idempotentlik", "fingerprint", "core", "idempotencyrecord"),
        ],
    },
    {
        "title": "Xavfsizlik",
        "separator": True,
        "collapsible": True,
        "items": [
            _item("Xodimlar", "badge", "accounts", "staffuser"),
            _item("Sessiyalar", "devices", "accounts", "authsession"),
            _item("Qurilmalar", "smartphone", "accounts", "device"),
            _item("SMS kodlar", "pin", "accounts", "smscode"),
            _item("2FA qurilmalar", "phonelink_lock", "otp_totp", "totpdevice"),
            _item("Tiklash kodlari", "key", "otp_static", "staticdevice"),
            _item("Xodim ijtimoiy kirishlari", "link", "accounts", "socialidentity"),
            _item("Audit jurnali", "manage_search", "core", "auditentry"),
        ],
    },
]

UNFOLD: dict[str, Any] = {
    "SITE_TITLE": "Glow Derma Ops",
    "SITE_HEADER": "Glow Derma Ops",
    "SITE_SUBHEADER": "Operatsion konsol",
    "SITE_URL": None,
    "SITE_ICON": lambda request: static("ops/logo.svg"),
    "SITE_FAVICONS": [
        {"rel": "icon", "type": "image/svg+xml", "href": lambda request: static("ops/favicon.svg")},
    ],
    # The sign-in page's side panel (wide screens): the brand's ink and gold, served from static.
    "LOGIN": {"image": lambda request: static("ops/login-side.svg")},
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "SHOW_BACK_BUTTON": True,
    "BORDER_RADIUS": "8px",
    "ENVIRONMENT": "glowderma.core.admin_site.environment_callback",
    "ENVIRONMENT_TITLE_PREFIX": "glowderma.core.admin_site.environment_title_prefix",
    "STYLES": [lambda request: static("ops/admin.css")],
    "COLORS": {
        "base": {
            "50": "oklch(98.4% .006 84.6)",
            "100": "oklch(96.2% .011 84.6)",
            "200": "oklch(91.7% .021 87.2)",
            "300": "oklch(85.0% .025 85.0)",
            "400": "oklch(68.3% .032 84.6)",
            "500": "oklch(54.9% .031 83.5)",
            "600": "oklch(44.5% .027 80.0)",
            "700": "oklch(35.5% .022 78.0)",
            "800": "oklch(27.2% .015 72.3)",
            "900": "oklch(21.0% .012 70.0)",
            "950": "oklch(15.0% .010 70.0)",
        },
        "primary": {
            "50": "oklch(97.7% .018 88.0)",
            "100": "oklch(94.2% .037 88.5)",
            "200": "oklch(89.5% .062 87.5)",
            "300": "oklch(85.2% .095 87.0)",
            "400": "oklch(81.2% .110 85.0)",
            "500": "oklch(73.1% .115 85.0)",
            "600": "oklch(55.5% .102 82.0)",
            "700": "oklch(48.0% .090 80.0)",
            "800": "oklch(40.0% .074 78.0)",
            "900": "oklch(33.0% .058 76.0)",
            "950": "oklch(24.0% .040 75.0)",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": True,
        "navigation": SIDEBAR_NAVIGATION,
    },
    "COMMAND": {"search_models": False, "show_history": False},
    "DASHBOARD_CALLBACK": f"{DASHBOARD_VIEWS}.dashboard_callback",
    # (url under /django-admin/, url name in the admin namespace, view class): Unfold wraps each in
    # ``admin_site.admin_view`` and passes ``admin_site`` to ``as_view``.
    "SITE_VIEWS": [
        ("dashboards/money/", "reporting_money", f"{DASHBOARD_VIEWS}.MoneyDashboardView"),
        ("dashboards/sales/", "reporting_sales", f"{DASHBOARD_VIEWS}.SalesDashboardView"),
        ("dashboards/customers/", "reporting_customers", f"{DASHBOARD_VIEWS}.CustomersDashboardView"),
    ],
}
