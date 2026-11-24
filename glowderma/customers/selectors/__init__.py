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

The customer API reads (customer contract §2.2, §2.3) live beside them: ``account`` (activity, the profile
and addresses) and ``catalog`` (categories, banners, products). ``common`` holds the media and translation
helpers. Callers import this facade.
"""

from glowderma.customers.selectors.account.activity import (
    is_dormant,
    last_activity_at,
)
from glowderma.customers.selectors.account.profile import (
    address_payload,
    customer_addresses,
    me_payload,
)
from glowderma.customers.selectors.admin.card import (
    CARD_DEVICES_LIMIT,
    DEVICE_COLUMNS,
    CustomerCard,
    CustomerOrders,
    DeviceInfo,
    Group,
    OnlineOrders,
    active_device,
    customer_card,
    customer_devices,
    customer_exists,
    customer_groups,
    customer_orders,
    get_customer,
    ledger_entry,
    newest_order,
)
from glowderma.customers.selectors.admin.listing import (
    DEFAULT_ORDERING,
    EMAIL_SEARCH_MIN_LENGTH,
    ORDERINGS,
    PHONE_SEARCH_MIN_DIGITS,
    SEARCH_MAX_LENGTH,
    customers_list,
    search_q,
)
from glowderma.customers.selectors.catalog.banners import (
    BANNER_LIMIT,
    banner_payload,
    customer_banners,
)
from glowderma.customers.selectors.catalog.categories import (
    active_category,
    category_payload,
    customer_categories,
)
from glowderma.customers.selectors.catalog.products import (
    customer_products,
    filter_in_stock,
    order_customer_products,
    product_cards,
    product_detail,
    resolved_name_expression,
    visible_product_ids,
)
from glowderma.customers.selectors.common import (
    NAME_COLLATION,
    localized_field,
    media_url,
    media_urls,
    product_name,
    seo_payload,
)

__all__ = [
    "BANNER_LIMIT",
    "CARD_DEVICES_LIMIT",
    "DEFAULT_ORDERING",
    "DEVICE_COLUMNS",
    "EMAIL_SEARCH_MIN_LENGTH",
    "NAME_COLLATION",
    "ORDERINGS",
    "PHONE_SEARCH_MIN_DIGITS",
    "SEARCH_MAX_LENGTH",
    "CustomerCard",
    "CustomerOrders",
    "DeviceInfo",
    "Group",
    "OnlineOrders",
    "active_category",
    "active_device",
    "address_payload",
    "banner_payload",
    "category_payload",
    "customer_addresses",
    "customer_banners",
    "customer_card",
    "customer_categories",
    "customer_devices",
    "customer_exists",
    "customer_groups",
    "customer_orders",
    "customer_products",
    "customers_list",
    "filter_in_stock",
    "get_customer",
    "is_dormant",
    "last_activity_at",
    "ledger_entry",
    "localized_field",
    "me_payload",
    "media_url",
    "media_urls",
    "newest_order",
    "order_customer_products",
    "product_cards",
    "product_detail",
    "product_name",
    "resolved_name_expression",
    "search_q",
    "seo_payload",
    "visible_product_ids",
]
