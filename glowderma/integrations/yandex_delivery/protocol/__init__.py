"""Yandex Delivery Express API protocol (``https://yandex.com/support/delivery-profile/en/api/express``,
revision r22073422, read 2026-11-15) — pure helpers, no database, no HTTP.

Every request goes to ``https://b2b.taxi.yandex.net`` with ``Authorization: Bearer <OAuth token>`` and the
required ``Accept-Language`` (``ru`` or ``en``). Ids travel in the query (``claim_id``, ``request_id``), the
rest in a JSON body. Error answers are ``{"code", "message"}``.

Endpoints (all ``POST`` under :data:`PREFIX` except ``tracking-links``, which is ``GET``):

* ``tariffs`` ``{start_point: [lon, lat], fullname}`` → ``available_tariffs[]``;
* ``check-price`` ``{route_points[{id, coordinates, fullname}], items[], requirements{taxi_class}}`` →
  ``price`` (Decimal(18, 4) string), ``currency_rules.code``, ``eta`` (minutes), ``distance_meters``,
  ``zone_id``; a non-binding estimate;
* ``claims/create?request_id=`` → the claim; ``claims/info?claim_id=``; ``claims/bulk_info {claim_ids}``
  (1–1000) → ``{claims: []}``;
* ``claims/accept?claim_id= {version}``; ``claims/cancel-info?claim_id=`` → ``cancel_state``, ``price``,
  ``price_with_vat``, ``currency``; ``claims/cancel?claim_id= {version, cancel_state}``;
  ``claims/return?claim_id= {point_id, comment, need_return_items: true}`` (200 without a body; the flag is
  the documented "return the item to the initial point of dispatch", as in the documented example);
* ``GET claims/tracking-links?claim_id=`` → ``route_points[{type, sharing_link}]``.

Coordinates are ``[longitude, latitude]`` — exactly that order — and always sent. The item's destination
field is spelled ``droppof_point`` in the English ``claims/create`` schema (required there); the Russian
schema prefers ``dropoff_point`` and keeps ``droppof_point`` as deprecated, so only ``droppof_point`` is sent
(accepted by both; a question for the account manager) and either spelling is read. ``check-price`` items use
``dropoff_point`` in both schemas.

Money: Yandex sends decimal strings in the claim's ``pricing.currency``; we keep integer so'm rounded **up**
(``Decimal`` → ceiling, never float) and refuse any other currency than ``YANDEX_DELIVERY_CURRENCY``.

The package: ``endpoints`` (paths and documented limits), ``values`` (money, phones, claim ids and the
readers of loose JSON), ``request_payloads`` and ``answer_parsers``.
"""

from glowderma.integrations.yandex_delivery.protocol.answer_parsers import (
    bulk_entries,
    cancel_terms_from,
    claim_from,
    claim_identity,
    error_message,
    estimate_from,
    sharing_link_from,
    tariffs_from,
)
from glowderma.integrations.yandex_delivery.protocol.endpoints import (
    COMMENT_MAX,
    CREATE_FORBIDDEN_CODES,
    DESTINATION_POINT_ID,
    LANGUAGES,
    MAX_PRICE,
    ORDER_ID_MAX,
    PATH_ACCEPT,
    PATH_BULK_INFO,
    PATH_CANCEL,
    PATH_CANCEL_INFO,
    PATH_CHECK_PRICE,
    PATH_CREATE,
    PATH_INFO,
    PATH_RETURN,
    PATH_TARIFFS,
    PATH_TRACKING_LINKS,
    PREFIX,
    REQUEST_ID_MAX,
    RETURN_COMMENT_MAX,
    SOURCE_POINT_ID,
    TEXT_MAX,
)
from glowderma.integrations.yandex_delivery.protocol.request_payloads import (
    cancel_payload,
    check_price_payload,
    create_payload,
    error_code,
    return_payload,
    tariffs_payload,
)
from glowderma.integrations.yandex_delivery.protocol.values import (
    CurrencyMismatch,
    ShapeError,
    check_currency,
    chunks,
    coordinates,
    normalise_phone,
    optional_so_m,
    so_m,
    valid_claim_id,
)

__all__ = [
    "COMMENT_MAX",
    "CREATE_FORBIDDEN_CODES",
    "DESTINATION_POINT_ID",
    "LANGUAGES",
    "MAX_PRICE",
    "ORDER_ID_MAX",
    "PATH_ACCEPT",
    "PATH_BULK_INFO",
    "PATH_CANCEL",
    "PATH_CANCEL_INFO",
    "PATH_CHECK_PRICE",
    "PATH_CREATE",
    "PATH_INFO",
    "PATH_RETURN",
    "PATH_TARIFFS",
    "PATH_TRACKING_LINKS",
    "PREFIX",
    "REQUEST_ID_MAX",
    "RETURN_COMMENT_MAX",
    "SOURCE_POINT_ID",
    "TEXT_MAX",
    "CurrencyMismatch",
    "ShapeError",
    "bulk_entries",
    "cancel_payload",
    "cancel_terms_from",
    "check_currency",
    "check_price_payload",
    "chunks",
    "claim_from",
    "claim_identity",
    "coordinates",
    "create_payload",
    "error_code",
    "error_message",
    "estimate_from",
    "normalise_phone",
    "optional_so_m",
    "return_payload",
    "sharing_link_from",
    "so_m",
    "tariffs_from",
    "tariffs_payload",
    "valid_claim_id",
]
