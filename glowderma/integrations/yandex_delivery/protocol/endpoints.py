"""The Yandex Delivery endpoints and the documented limits of their fields."""

from __future__ import annotations

from typing import Final

PREFIX: Final = "/b2b/cargo/integration/v2"


PATH_TARIFFS: Final = f"{PREFIX}/tariffs"


PATH_CHECK_PRICE: Final = f"{PREFIX}/check-price"


PATH_CREATE: Final = f"{PREFIX}/claims/create"


PATH_INFO: Final = f"{PREFIX}/claims/info"


PATH_BULK_INFO: Final = f"{PREFIX}/claims/bulk_info"


PATH_ACCEPT: Final = f"{PREFIX}/claims/accept"


PATH_CANCEL_INFO: Final = f"{PREFIX}/claims/cancel-info"


PATH_CANCEL: Final = f"{PREFIX}/claims/cancel"


PATH_RETURN: Final = f"{PREFIX}/claims/return"


PATH_TRACKING_LINKS: Final = f"{PREFIX}/claims/tracking-links"


LANGUAGES: Final = frozenset({"ru", "en"})


SOURCE_POINT_ID: Final = 1


DESTINATION_POINT_ID: Final = 2


# Documented limits: request_id 1–128, comments 7000, external_order_id / extra_id 512, claim id 32–64.
REQUEST_ID_MAX: Final = 128


COMMENT_MAX: Final = 7000


ORDER_ID_MAX: Final = 512


RETURN_COMMENT_MAX: Final = 1000


# The largest price we accept (so'm); anything above is not a delivery price.
MAX_PRICE: Final = 100_000_000


# claims/create's documented 403 codes: definitive refusals of the claim (the account's anti-fraud check, a
# payment option the contract lacks), not the "token / account, try later" 403 of any other answer.
CREATE_FORBIDDEN_CODES: Final = frozenset(
    {"forbidden_by_antifake", "buyout_disabled", "payment_on_delivery_disabled"}
)


TEXT_MAX: Final = 120


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
]
