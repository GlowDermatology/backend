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
the same warm taupe). ``primary-600`` (Unfold's text colour of links and the active sidebar entry) is a shade
darker than the gold ink so that it reads at WCAG AA 4.5:1 on ``base-100`` (the active entry's background).
"""

from typing import TYPE_CHECKING, Any

from django.templatetags.static import static
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

STAFF_ONLY = "glowderma.core.admin.is_active_staff"
NAV_BADGES = "glowderma.reporting.admin_nav"


def _changelist(app_label: str, model_name: str) -> Any:
    return reverse_lazy(f"admin:{app_label}_{model_name}_changelist")


def _item(
    title: StrOrPromise, icon: str, app_label: str, model_name: str, badge: str | None = None
) -> dict[str, Any]:
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


def _dashboard(title: StrOrPromise, icon: str, url_name: str) -> dict[str, Any]:
    return {"title": title, "icon": icon, "link": reverse_lazy(url_name), "permission": STAFF_ONLY}


# The four dashboards of the console (glowderma.reporting.dashboards): the index is «Ops holati».
DASHBOARD_GROUP: dict[str, Any] = {
    "title": _("Boshqaruv"),
    "separator": False,
    "collapsible": False,
    "items": [
        _dashboard(_("Ops holati"), "monitor_heart", "admin:index"),
        _dashboard(_("Pul solishtirish"), "account_balance", "admin:reporting_money"),
        _dashboard(_("Savdo va katalog"), "query_stats", "admin:reporting_sales"),
        _dashboard(_("Mijozlar va kirish"), "diversity_3", "admin:reporting_customers"),
    ],
}

SIDEBAR_NAVIGATION: list[dict[str, Any]] = [
    DASHBOARD_GROUP,
    {
        "title": _("Savdo"),
        "separator": True,
        "collapsible": False,
        "items": [
            _item(_("Buyurtmalar"), "shopping_bag", "commerce", "order"),
            _item(_("To‘lovlar"), "payments", "payments", "payment"),
            _item(
                _("Qaytarishlar"), "currency_exchange", "payments", "refund", badge="refunds_needing_action"
            ),
            _item(_("Yetkazib berish"), "local_shipping", "delivery", "shipment"),
            _item(_("Savatlar"), "shopping_cart", "commerce", "cart"),
            _item(_("Buyurtma qatorlari"), "list_alt", "commerce", "orderitem"),
            _item(_("Buyurtma tarixi"), "history", "commerce", "orderevent"),
            _item(_("Zaxira bandlari"), "inventory", "commerce", "stockreservation"),
            _item(_("Savat qatorlari"), "add_shopping_cart", "commerce", "cartitem"),
            _item(_("Yetkazish tarixi"), "route", "delivery", "shipmentevent"),
        ],
    },
    {
        "title": _("Katalog"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Mahsulotlar"), "inventory_2", "catalog", "product"),
            _item(_("Kategoriyalar"), "category", "catalog", "category"),
            _item(_("Qoldiqlar"), "warehouse", "catalog", "productstock"),
            _item(_("Rasmlar"), "image", "catalog", "productimage"),
            _item(_("Bannerlar"), "view_carousel", "catalog", "banner"),
            # The settings page (glowderma.site_config.admin_views): store settings, contacts, legal texts.
            _dashboard(_("Sozlamalar"), "tune", "admin:site_config_settings"),
        ],
    },
    {
        "title": _("Mijozlar va sodiqlik"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Mijozlar"), "group", "accounts", "customer"),
            _item(_("Manzillar"), "home_pin", "accounts", "address"),
            _item(_("Ijtimoiy kirishlar"), "account_circle", "accounts", "customersocialidentity"),
            _item(_("Cashback hisoblari"), "account_balance_wallet", "loyalty", "loyaltyaccount"),
            _item(_("Cashback daftari"), "receipt_long", "loyalty", "ledgerentry"),
            _item(_("Cashback bandlari"), "lock_clock", "loyalty", "cashbackhold"),
            _item(_("Xaridlar"), "storefront", "loyalty", "purchase"),
            _item(_("Xarid qatorlari"), "format_list_bulleted", "loyalty", "purchaseitem"),
            _item(_("Cashback siyosati"), "policy", "loyalty", "cashbackpolicy"),
            _item(_("Cashback darajalari"), "stairs", "loyalty", "cashbacktier"),
            _item(_("POS qurilmalar"), "point_of_sale", "loyalty", "posdevice"),
        ],
    },
    {
        "title": _("Klinika"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Qabullar"), "event", "appointments", "appointment"),
            _item(_("Qabul tarixi"), "event_note", "appointments", "appointmentevent"),
            _item(_("Mutaxassislar"), "medical_services", "professionals", "professional"),
            _item(_("Xizmatlar"), "spa", "professionals", "service"),
            _item(_("Mutaxassis xizmatlari"), "assignment_ind", "professionals", "professionalservice"),
            _item(_("Ish vaqti"), "schedule", "professionals", "workinghours"),
            _item(_("Kun ish vaqti"), "calendar_month", "professionals", "workinghoursoverride"),
            _item(_("Band vaqtlar"), "block", "professionals", "blockedperiod"),
            _item(_("Filiallar"), "location_on", "professionals", "location"),
        ],
    },
    {
        "title": _("Xabarnomalar"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Kampaniyalar"), "campaign", "notifications", "campaign"),
            _item(_("Qabul qiluvchilar"), "group_add", "notifications", "campaignrecipient"),
            _item(_("Inbox"), "inbox", "notifications", "inboxnotification"),
            _item(_("Segmentlar"), "groups", "notifications", "customersegment"),
            _item(_("Segment a’zolari"), "person_add", "notifications", "segmentmember"),
            _item(_("Holat shablonlari"), "sms", "notifications", "statustemplate"),
        ],
    },
    {
        "title": _("Integratsiyalar va navbatlar"),
        "separator": True,
        "collapsible": False,
        "items": [
            _item("Outbox", "outbox", "core", "outboxmessage", badge="dead_outbox"),
            _item(_("Webhooklar"), "webhook", "integrations", "webhookinbox", badge="failed_webhooks"),
            _item(_("Fon vazifalari"), "sync", "integrations", "job"),
            _item(_("Integratsiya holati"), "hub", "integrations", "integrationstate"),
            _item(_("Idempotentlik"), "fingerprint", "core", "idempotencyrecord"),
        ],
    },
    {
        "title": _("Xavfsizlik"),
        "separator": True,
        "collapsible": True,
        "items": [
            _item(_("Xodimlar"), "badge", "accounts", "staffuser"),
            _item(_("Sessiyalar"), "devices", "accounts", "authsession"),
            _item(_("Qurilmalar"), "smartphone", "accounts", "device"),
            _item(_("SMS kodlar"), "pin", "accounts", "smscode"),
            _item(_("2FA qurilmalar"), "phonelink_lock", "otp_totp", "totpdevice"),
            _item(_("Tiklash kodlari"), "key", "otp_static", "staticdevice"),
            _item(_("Xodim ijtimoiy kirishlari"), "link", "accounts", "socialidentity"),
            _item(_("Audit jurnali"), "manage_search", "core", "auditentry"),
        ],
    },
]

UNFOLD: dict[str, Any] = {
    "SITE_TITLE": "Glow Derma Ops",
    "SITE_HEADER": "Glow Derma Ops",
    "SITE_SUBHEADER": _("Operatsion konsol"),
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
            "600": "oklch(53.0% .102 82.0)",
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
    # The user menu: the user's own staff record instead of Unfold's stock "Change password" link (the
    # console has no password change; glowderma.core.admin_site).
    "ACCOUNT": {"navigation": "glowderma.core.admin_site.account_links"},
    "DASHBOARD_CALLBACK": f"{DASHBOARD_VIEWS}.dashboard_callback",
    # (url under /django-admin/, url name in the admin namespace, view class): Unfold wraps each in
    # ``admin_site.admin_view`` and passes ``admin_site`` to ``as_view``.
    "SITE_VIEWS": [
        ("dashboards/money/", "reporting_money", f"{DASHBOARD_VIEWS}.MoneyDashboardView"),
        ("dashboards/sales/", "reporting_sales", f"{DASHBOARD_VIEWS}.SalesDashboardView"),
        ("dashboards/customers/", "reporting_customers", f"{DASHBOARD_VIEWS}.CustomersDashboardView"),
        # The store settings, contacts and legal texts (moved here from the Next.js admin: 2FA-protected).
        ("settings/", "site_config_settings", "glowderma.site_config.admin_views.SettingsConsoleView"),
    ],
}
