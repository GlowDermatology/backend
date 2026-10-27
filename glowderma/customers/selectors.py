"""Read side of `customers` (contract §2.6, design 03 §6.3): the admin customers list and the customer card.

`customers` owns no tables (decision T2): it sits above `loyalty`, `commerce`, `appointments` and
`notifications` and aggregates them. Every loyalty number comes from ``loyalty.selectors``: the list orders
by the SQL annotations of :func:`loyalty.selectors.list_annotations` (so sorting and paging happen in the
database), the card reads ``customer_totals`` / ``account_summary`` / ``tier_for`` / ``ledger_for_customer`` /
``purchases_for_customer``; the list's segment filter reuses ``notifications.selectors.segment_member_ids``,
the card's groups evaluate the same membership rule in one query (:func:`customer_groups`), and the upcoming
appointments come from ``appointments.selectors.upcoming_count``.

Three reads have no selector in their owning module yet and are implemented here against the models
(reported to the lead): the customer row itself (``accounts`` has no selectors module), the active device
(design 01 §1.2 ``accounts.selectors.primary_device``) and the newest order of a customer (``commerce`` has no
selectors module yet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, Exists, F, Max, OrderBy, OuterRef, Q, QuerySet, Value, Window
from django.db.models.functions import Coalesce, Collate, NullIf
from django.db.models.lookups import Contains

from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.appointments import selectors as appointments_selectors
from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import Banner, Category, Product, ProductImage
from glowderma.commerce import selectors as commerce_selectors
from glowderma.commerce.models import CartItem, Order
from glowderma.core import enums, i18n, timeutils
from glowderma.core.media import absolute_media_url
from glowderma.core.search import SearchFold
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty.models import Purchase
from glowderma.loyalty.selectors import AccountSummary, LedgerRow, PurchaseRow, TierInfo, Totals
from glowderma.notifications import selectors as notifications_selectors
from glowderma.notifications.models import CustomerSegment, SegmentMember
from glowderma.site_config import selectors as site_config_selectors

NAME_COLLATION = "glowderma_ci"  # ICU case-insensitive collation (core.0007), used in ORDER BY only
SEARCH_MAX_LENGTH = 64
PHONE_SEARCH_MIN_DIGITS = 3
EMAIL_SEARCH_MIN_LENGTH = 3
DEFAULT_ORDERING = "-registered_at"


def _name_ci() -> Collate:
    return Collate(F("name"), NAME_COLLATION)


# ``ordering`` value → ORDER BY terms; ties by ``id`` in the same direction (design 03 §6.3).
ORDERINGS: dict[str, tuple[OrderBy, ...]] = {
    "-registered_at": (F("registered_at").desc(), F("id").desc()),
    "registered_at": (F("registered_at").asc(), F("id").asc()),
    "name": (_name_ci().asc(), F("id").asc()),
    "-name": (_name_ci().desc(), F("id").desc()),
    "lifetime_total": (F("lifetime_total").asc(), F("id").asc()),
    "-lifetime_total": (F("lifetime_total").desc(), F("id").desc()),
    "balance": (F("balance").asc(), F("id").asc()),
    "-balance": (F("balance").desc(), F("id").desc()),
}


# --- the list (contract §2.6 ``GET customers/``) -------------------------------------------------------


def search_q(term: str) -> Q | None:
    """``name`` contains the term (case-insensitive and folded — apostrophe variants and ё, ``core.search``,
    decision GC-1: ``fold(name) LIKE '%' || fold(term) || '%'``, served by the trigram index
    ``accounts_customer_fold_trgm``), or, when the term carries at least three digits, the phone contains
    those digits, or (hybrid identity) the e-mail address contains the term (case-insensitive). Blank → no
    filter."""
    text = term.strip()
    if not text:
        return None
    condition = Q(Contains(SearchFold(F("name")), SearchFold(Value(text))))
    digits = re.sub(r"\D", "", text)
    if len(digits) >= PHONE_SEARCH_MIN_DIGITS:
        condition |= Q(phone__contains=digits)
    if len(text) >= EMAIL_SEARCH_MIN_LENGTH:
        condition |= Q(email__contains=text.lower())  # stored lower case
    return condition


def customers_list(
    *,
    search: str | None = None,
    status: str | None = None,
    segment_id: int | None = None,
    ordering: str = DEFAULT_ORDERING,
) -> QuerySet[Customer]:
    """The filtered, ordered customer rows annotated with ``lifetime_total``, ``balance``,
    ``online_orders_count`` and ``percent`` (the active policy's rate for ``lifetime_total`` | ``None``)."""
    if ordering not in ORDERINGS:
        raise ValueError(f"Unknown customers ordering {ordering!r}")
    qs = Customer.objects.annotate(**loyalty_selectors.list_annotations())
    if status is not None:
        qs = qs.filter(status=status)
    if search is not None:
        condition = search_q(search)
        if condition is not None:
            qs = qs.filter(condition)
    if segment_id is not None:
        segment = CustomerSegment.objects.filter(pk=segment_id).first()
        if segment is None:
            return qs.none()
        qs = qs.filter(pk__in=notifications_selectors.segment_member_ids(segment))
    return qs.order_by(*ORDERINGS[ordering])


# --- the card (contract §2.6 ``GET customers/{id}/``) --------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnlineOrders:
    count: int  # paid, non-reversed online purchases (what the loyalty programme counts)
    last_order_id: int | None  # the customer's newest order of any status (contract §7 item 18)
    last_order_at: datetime | None
    all_count: int  # every order of any status: the rows of ``orders/?customer={id}`` (gap ADM-8.1)


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One installation of the app as the card shows it: never the FCM token, the device key hash or the
    Firebase installation id."""

    id: int
    platform: str
    model: str
    os: str
    app_version: str
    last_active_at: datetime
    push_enabled: bool
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerCard:
    """Contract §2.6 ``CustomerCard``."""

    id: int
    name: str
    phone: str | None  # hybrid identity: null until the customer attaches one
    email: str | None
    status: str
    registered_at: datetime
    deleted_at: datetime | None
    language: str
    address: str | None
    totals: Totals
    online_orders: OnlineOrders
    tier: TierInfo | None
    balance: int
    held: int
    available: int
    groups: list[Group]
    upcoming_appointments: int
    device: DeviceInfo | None
    devices: list[DeviceInfo]
    ledger: list[LedgerRow]
    purchases: list[PurchaseRow]


def get_customer(customer_id: int) -> Customer | None:
    """The customer row of any status (deleted customers stay addressable), or ``None``."""
    return Customer.objects.filter(pk=customer_id).first()


def customer_exists(customer_id: int) -> bool:
    return Customer.objects.filter(pk=customer_id).exists()


@dataclass(frozen=True, slots=True)
class CustomerOrders:
    count: int  # every order of any status
    newest: tuple[int, datetime] | None  # ``(id, created_at)`` of the newest one


def customer_orders(customer_id: int) -> CustomerOrders:
    """How many orders the customer has (any status) and the newest one (``created_at desc, id desc``), in
    one query: the count is a window over the same rows as the newest row."""
    row = (
        Order.objects.filter(customer_id=customer_id)
        .annotate(order_count=Window(Count("id")))
        .order_by("-created_at", "-id")
        .values_list("id", "created_at", "order_count")
        .first()
    )
    if row is None:
        return CustomerOrders(count=0, newest=None)
    return CustomerOrders(count=int(row[2]), newest=(int(row[0]), row[1]))


def newest_order(customer_id: int) -> tuple[int, datetime] | None:
    """``(id, created_at)`` of the customer's newest order of any status (``created_at desc, id desc``)."""
    return customer_orders(customer_id).newest


# The card lists the most recently active devices first, at most this many (decision GST-7).
CARD_DEVICES_LIMIT = 20
DEVICE_COLUMNS = (
    "id",
    "platform",
    "model",
    "os",
    "app_version",
    "last_active_at",
    "push_enabled",
    "is_active",
    "created_at",
)


def customer_devices(customer_id: int) -> list[DeviceInfo]:
    """The customer's devices, active ones first, then by ``last_active_at`` (newest first), ties by id; at
    most :data:`CARD_DEVICES_LIMIT`. One query; the secret columns are never read."""
    rows = (
        Device.objects.filter(customer_id=customer_id)
        .order_by("-is_active", "-last_active_at", "-id")
        .only(*DEVICE_COLUMNS)[:CARD_DEVICES_LIMIT]
    )
    return [
        DeviceInfo(
            id=device.pk,
            platform=device.platform,
            model=device.model,
            os=device.os,
            app_version=device.app_version,
            last_active_at=device.last_active_at,
            push_enabled=device.push_enabled,
            is_active=device.is_active,
            created_at=device.created_at,
        )
        for device in rows
    ]


def _current(devices: list[DeviceInfo]) -> DeviceInfo | None:
    """The card's ``device``: the most recently active of the active devices (the first of the list)."""
    return devices[0] if devices and devices[0].is_active else None


def active_device(customer_id: int) -> DeviceInfo | None:
    """The customer's active device (the most recently active one if several), or ``None``."""
    return _current(customer_devices(customer_id))


def customer_groups(customer: Customer, totals: Totals) -> list[Group]:
    """The segments the customer belongs to (rules or manual membership, contract §2.8), by id; a deleted
    customer has none (membership requires an active customer).

    One query whatever the number of segments (design 03 §6.3, no N+1): every segment annotated with whether
    the customer is a manual member, the rules checked in Python against the card's ``totals``.
    It reuses the card's ``totals`` instead of re-reading the stats (``notifications.selectors.rules_hold``
    is the shared rule check; ``segments_for_customer`` answers the same set for callers without totals).
    """
    if customer.status != enums.CustomerStatus.ACTIVE:
        return []
    segments = (
        CustomerSegment.objects.order_by("id")
        .only("id", "name", "min_online_orders", "min_lifetime_amount")
        .annotate(
            is_manual_member=Exists(
                SegmentMember.objects.filter(segment_id=OuterRef("pk"), customer_id=customer.pk)
            )
        )
    )
    return [
        Group(id=segment.pk, name=segment.name)
        for segment in segments
        if getattr(segment, "is_manual_member", False)
        or notifications_selectors.rules_hold(
            segment, online_orders_count=totals.online_orders_count, lifetime=totals.lifetime
        )
    ]


def customer_card(customer_id: int) -> CustomerCard | None:
    """The full card of a customer of any status, or ``None`` when the id is unknown."""
    customer = get_customer(customer_id)
    if customer is None:
        return None
    totals = loyalty_selectors.customer_totals(customer.pk)
    summary: AccountSummary = loyalty_selectors.account_summary(customer.pk)
    orders = customer_orders(customer.pk)
    last = orders.newest
    devices = customer_devices(customer.pk)
    return CustomerCard(
        id=customer.pk,
        name=customer.name,
        phone=customer.phone,
        email=customer.email,
        status=customer.status,
        registered_at=customer.registered_at,
        deleted_at=customer.deleted_at,
        language=customer.language,
        address=customer.address_text or None,
        totals=totals,
        online_orders=OnlineOrders(
            count=totals.online_orders_count,
            last_order_id=None if last is None else last[0],
            last_order_at=None if last is None else last[1],
            all_count=orders.count,
        ),
        tier=loyalty_selectors.tier_for(totals.lifetime),
        balance=summary.balance,
        held=summary.held,
        available=summary.available,
        groups=customer_groups(customer, totals),
        upcoming_appointments=appointments_selectors.upcoming_count(customer.pk),
        device=_current(devices),
        devices=devices,
        ledger=loyalty_selectors.ledger_for_customer(customer.pk),
        purchases=loyalty_selectors.purchases_for_customer(customer.pk),
    )


def ledger_entry(entry_id: int) -> LedgerRow:
    """One ledger entry in the §2.6 shape (the adjustment response)."""
    return loyalty_selectors.ledger_entry(entry_id)


__all__ = [
    "CARD_DEVICES_LIMIT",
    "DEFAULT_ORDERING",
    "ORDERINGS",
    "CustomerCard",
    "CustomerOrders",
    "DeviceInfo",
    "Group",
    "OnlineOrders",
    "active_device",
    "customer_card",
    "customer_devices",
    "customer_exists",
    "customer_groups",
    "customer_orders",
    "customers_list",
    "get_customer",
    "ledger_entry",
    "newest_order",
    "search_q",
]


# --- M2 customer API reads (customer contract §2.2, §2.3; plan §2 P1) --------------------------------------


def last_activity_at(customer_id: int) -> datetime | None:
    """The dormant-account rule's last activity (customer contract §2.2.3): the newest of
    ``Customer.last_login_at``, the newest customer session ``last_used_at``, the newest ``Purchase``, the
    newest order and the appointment activity of ``appointments.selectors.last_activity_at`` (every
    appointment's ``created_at``, staff bookings included, and the ``start`` of open or attended ones)."""
    candidates: list[datetime | None] = [
        Customer.objects.filter(pk=customer_id).values_list("last_login_at", flat=True).first(),
        AuthSession.objects.filter(customer_id=customer_id, audience="customer").aggregate(
            newest=Max("last_used_at")
        )["newest"],
        Purchase.objects.filter(customer_id=customer_id).aggregate(newest=Max("at"))["newest"],
        Order.objects.filter(customer_id=customer_id).aggregate(newest=Max("created_at"))["newest"],
        appointments_selectors.last_activity_at(customer_id),
    ]
    values = [value for value in candidates if value is not None]
    return max(values) if values else None


def is_dormant(customer_id: int, *, now: datetime) -> tuple[bool, datetime | None]:
    """``(dormant, last_activity_at)``: inactive for longer than ``CUSTOMER_DORMANT_DAYS`` (no activity at all
    counts as dormant)."""
    last = last_activity_at(customer_id)
    threshold = now - timedelta(days=int(settings.CUSTOMER_DORMANT_DAYS))
    return (last is None or last < threshold), last


def media_url(file: Any) -> str | None:
    """The absolute URL of a stored file (``PUBLIC_MEDIA_BASE_URL``), ``None`` when empty."""
    name = getattr(file, "name", None)
    return absolute_media_url(str(name)) if name else None


def localized_field(obj: object, field: str, lang: str) -> str:
    """``<field>_<lang>`` → ``<field>_uz`` → ``""`` (customer contract §1.4)."""
    return str(getattr(obj, f"{field}_{lang}", "") or getattr(obj, f"{field}_uz", "") or "")


def product_name(product: Product, lang: str) -> str:
    """The resolved-language name, then uz, then ``billz_name``."""
    return localized_field(product, "name", lang) or product.billz_name


def address_payload(address: Address) -> dict[str, Any]:
    return {
        "id": address.pk,
        "label": address.label,
        "text": address.text,
        "region_code": address.region_code,
        "city_code": address.city_code,
        "comment": address.comment,
        "latitude": float(address.latitude) if address.latitude is not None else None,
        "longitude": float(address.longitude) if address.longitude is not None else None,
        "is_default": address.is_default,
        "created_at": timeutils.isoformat(address.created_at),
        "updated_at": timeutils.isoformat(address.updated_at),
    }


def customer_addresses(customer_id: int) -> list[Address]:
    """Default first, then ``updated_at`` desc (customer contract §2.2.7)."""
    return list(Address.objects.filter(customer_id=customer_id).order_by("-is_default", "-updated_at", "-id"))


def me_payload(customer: Customer, *, fid: str | None) -> dict[str, Any]:
    """``Me`` (customer contract §2.2.1); ``push_enabled`` of the current device (``fid``)."""
    terms = site_config_selectors.current_terms()
    current = terms.version if terms.published else ""
    providers = sorted(
        CustomerSocialIdentity.objects.filter(customer_id=customer.pk).values_list("provider", flat=True)
    )
    device = Device.objects.filter(customer_id=customer.pk, fid=fid).first() if fid else None
    default = Address.objects.filter(customer_id=customer.pk, is_default=True).first()
    return {
        "id": customer.pk,
        "phone": customer.phone or None,
        "email": customer.email or None,
        "name": customer.name,
        "language": customer.language,
        "image_url": media_url(customer.image),
        "registered_at": timeutils.isoformat(customer.history_from or customer.registered_at),
        "has_password": bool(customer.password),
        "linked_providers": providers,
        "push_enabled": bool(device and device.push_enabled),
        "consent": {
            "terms_version": customer.terms_version or None,
            "accepted_at": timeutils.isoformat(customer.consent_at) if customer.consent_at else None,
            "current_version": current or None,
            "update_required": bool(current) and customer.terms_version != current,
        },
        "default_address": address_payload(default) if default is not None else None,
    }


# --- public catalog (customer contract §2.3) ---


def customer_categories() -> QuerySet[Category]:
    """Active categories by position; ``product_count`` = visible products."""
    visible = catalog_selectors.customer_visible_products()
    return (
        Category.objects.filter(is_active=True)
        .annotate(product_count=Count("products", filter=Q(products__in=visible), distinct=True))
        .order_by("position", "id")
    )


def active_category(slug: str) -> Category | None:
    return Category.objects.filter(slug=slug, is_active=True).first()


def customer_banners(now: datetime) -> list[Banner]:
    """``is_active`` ∧ started ∧ not ended, by position and id, at most 20."""
    return list(
        Banner.objects.select_related("link_product", "link_category")
        .filter(is_active=True)
        .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .order_by("position", "id")[:BANNER_LIMIT]
    )


BANNER_LIMIT = 20


def banner_payload(banner: Banner, lang: str, visible_ids: set[int]) -> dict[str, Any]:
    """A link to an invisible product or an inactive category renders ``kind: "none"``; a URL link
    (``kind: "url"``, gap X-BANNER-URL) carries the https ``url`` the app opens."""
    link: dict[str, Any] = {
        "kind": enums.BannerLinkKind.NONE.value,
        "product_id": None,
        "category": None,
        "url": None,
    }
    if banner.link_kind == enums.BannerLinkKind.PRODUCT and banner.link_product_id in visible_ids:
        link = {"kind": banner.link_kind, "product_id": banner.link_product_id, "category": None, "url": None}
    elif (
        banner.link_kind == enums.BannerLinkKind.CATEGORY
        and banner.link_category is not None
        and banner.link_category.is_active
    ):
        link = {
            "kind": banner.link_kind,
            "product_id": None,
            "category": banner.link_category.slug,
            "url": None,
        }
    elif banner.link_kind == enums.BannerLinkKind.URL and banner.link_url:
        link = {"kind": banner.link_kind, "product_id": None, "category": None, "url": banner.link_url}
    return {
        "id": banner.pk,
        "title": localized_field(banner, "title", lang),
        "image_url": media_url(banner.image),
        "image_width": banner.image_width,
        "image_height": banner.image_height,
        "link": link,
    }


def visible_product_ids(ids: list[int]) -> set[int]:
    return set(catalog_selectors.customer_visible_products().filter(pk__in=ids).values_list("pk", flat=True))


def customer_products() -> QuerySet[Product]:
    return catalog_selectors.customer_visible_products().select_related("category")


def resolved_name_expression(lang: str) -> Coalesce:
    columns = [f"name_{lang}"] if lang != enums.ContentLanguage.UZ else []
    parts = [NullIf(F(column), Value("")) for column in (*columns, "name_uz")]
    return Coalesce(*parts, F("billz_name"))


def order_customer_products(qs: QuerySet[Product], ordering: str, lang: str) -> QuerySet[Product]:
    """``default`` (newest ``created_at``), ``price``, ``-price``, ``name`` (resolved language under
    ``glowderma_ci``); ties by ``id`` desc."""
    if ordering == enums.CustomerProductOrdering.PRICE:
        return qs.order_by("price", "-id")
    if ordering == enums.CustomerProductOrdering.PRICE_DESC:
        return qs.order_by("-price", "-id")
    if ordering == enums.CustomerProductOrdering.NAME:
        return qs.annotate(_name_ci=Collate(resolved_name_expression(lang), NAME_COLLATION)).order_by(
            "_name_ci", "-id"
        )
    return qs.order_by("-created_at", "-id")


def filter_in_stock(qs: QuerySet[Product], in_stock: bool) -> QuerySet[Product]:
    """``available > 0`` (``customer_available_stock``: nothing while online selling is off)."""
    ids = list(qs.values_list("pk", flat=True))
    available = commerce_selectors.customer_available_stock(ids)
    positive = [pk for pk, quantity in available.items() if quantity > 0]
    return qs.filter(pk__in=positive) if in_stock else qs.exclude(pk__in=positive)


def _main_images(product_ids: list[int]) -> dict[int, ProductImage]:
    images: dict[int, ProductImage] = {}
    for image in ProductImage.objects.filter(product_id__in=product_ids).order_by(
        "product_id", "position", "id"
    ):
        images.setdefault(image.product_id, image)
    return images


def _cart_quantities(customer_id: int | None, product_ids: list[int]) -> dict[int, int]:
    if customer_id is None:
        return {}
    return dict(
        CartItem.objects.filter(cart__customer_id=customer_id, product_id__in=product_ids).values_list(
            "product_id", "quantity"
        )
    )


def product_cards(products: list[Product], lang: str, customer_id: int | None = None) -> list[dict[str, Any]]:
    """``ProductCard`` rows: one stock read (``customer_available_stock``), one image query, one cart
    query."""
    ids = [product.pk for product in products]
    available = commerce_selectors.customer_available_stock(ids)
    images = _main_images(ids)
    in_cart = _cart_quantities(customer_id, ids)
    cards = []
    for product in products:
        quantity = int(available.get(product.pk, 0))
        image = images.get(product.pk)
        category = product.category
        cards.append(
            {
                "id": product.pk,
                "slug": product.slug,
                "name": product_name(product, lang),
                "content_language": i18n.content_language(product, "name", lang, fallback_text=True),
                "volume": product.volume,
                "price": product.price,
                "promo": product.promo,
                "category": {"slug": category.slug, "name": localized_field(category, "name", lang)}
                if category is not None
                else None,
                "main_image_url": media_url(image.file) if image is not None else None,
                "available": quantity,
                "stock_status": (
                    enums.StockStatus.IN_STOCK if quantity > 0 else enums.StockStatus.OUT_OF_STOCK
                ).value,
                "in_cart_quantity": int(in_cart.get(product.pk, 0)),
                "updated_at": product.updated_at,
            }
        )
    return cards


def product_detail(product: Product, lang: str, customer_id: int | None = None) -> dict[str, Any]:
    [card] = product_cards([product], lang, customer_id)
    images = ProductImage.objects.filter(product_id=product.pk).order_by("position", "id")
    return {
        **card,
        "description": localized_field(product, "description", lang),
        "ingredients": localized_field(product, "ingredients", lang),
        "usage": localized_field(product, "usage", lang),
        "images": [
            {"id": image.pk, "url": media_url(image.file), "position": image.position} for image in images
        ],
        "max_quantity": min(card["available"], int(settings.CART_MAX_QTY_PER_LINE)),
        **seo_payload(product, lang),
    }


def seo_payload(obj: Any, lang: str) -> dict[str, str]:
    """``seo_title`` / ``seo_description`` (B-W1) in the resolved language only — never another language's
    text in a page's meta tags; ``""`` = the website builds them (name, volume, category, price)."""
    language = lang if lang in enums.ContentLanguage.values else enums.ContentLanguage.UZ
    return {
        "seo_title": str(getattr(obj, f"seo_title_{language}", "") or ""),
        "seo_description": str(getattr(obj, f"seo_description_{language}", "") or ""),
    }


def category_payload(category: Category, lang: str) -> dict[str, Any]:
    return {
        "slug": category.slug,
        "name": localized_field(category, "name", lang),
        "content_language": i18n.content_language(category, "name", lang),
        "image_url": media_url(category.image),
        "product_count": int(getattr(category, "product_count", 0)),
        "updated_at": category.updated_at,
        **seo_payload(category, lang),
    }
