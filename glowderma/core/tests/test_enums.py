from django.db.models import TextChoices
from drf_spectacular.plumbing import list_hash
from drf_spectacular.settings import spectacular_settings

from glowderma.core import enums

# Copied literally from docs/api/admin-api-v1.md section 3.1 (OrderEventType from section 4).
CONTRACT_TABLE: dict[str, list[str]] = {
    "OrderStatus": ["awaiting_payment", "processing", "handed_to_courier", "delivered", "cancelled"],
    "PaymentStatus": ["pending", "paid", "failed", "refunded"],
    "PaymentProvider": ["click", "payme"],
    "FulfilmentMethod": ["delivery", "pickup"],
    "OrderChannel": ["ios", "android"],
    "OrderPeriod": ["today", "week", "month", "all"],
    "EventSource": ["system", "bts", "admin"],
    "OrderEventType": [
        "order_created",
        "reservation_created",
        "payment_confirmed",
        "billz_sale_recorded",
        "cashback_credited",
        "payment_failed",
        "shipment_created",
        "shipment_failed",
        "status_changed",
        "handed_to_courier",
        "delivered",
        "cancelled",
        "reservation_expired",
        "refund_requested",
        "refund_succeeded",
        "refund_failed",
    ],
    "CancellationReason": ["reservation_expired", "admin", "customer_request"],
    "ShipmentState": ["not_required", "not_created", "failed", "created"],
    "ShipmentVia": ["webhook", "polling"],
    "RefundState": ["requested", "submitting", "succeeded", "failed", "manual_required", "unknown"],
    "CashbackHoldState": ["active", "captured", "released"],
    "OrderCashbackKind": ["debit", "credit", "reversal"],
    "LedgerKind": ["credit", "debit", "adjustment", "reversal"],
    "LedgerSourceType": ["order", "billz_sale", "admin"],
    "AdjustmentDirection": ["add", "subtract"],
    "PurchaseSource": ["online", "store_qr"],
    "RateBasis": ["before_purchase", "with_purchase"],
    "PolicyVersionState": ["active", "scheduled", "expired", "superseded"],
    "TierWarning": ["gap", "uncovered_above"],
    "ProductStatus": ["active", "hidden"],
    "ProductChangeKind": [
        "created",
        "price_changed",
        "activated",
        "hidden",
        "promo_added",
        "promo_removed",
        "updated",
    ],
    "ProductOrdering": ["default", "name", "-name"],
    "ContentLanguage": ["uz", "ru", "en"],
    "CustomerStatus": ["active", "deleted"],
    "SpecialistRole": ["dermatologist", "cosmetologist"],
    # "cancelled" is the extra M2 literal (customers cancel their own appointments).
    "AppointmentStatus": ["requested", "confirmed", "rejected", "attended", "cancelled"],
    "AppointmentAction": ["confirm", "reject", "attend"],
    "RequestedVia": ["app", "admin"],
    "BlockKind": ["busy", "off"],
    "NotificationKind": ["promo", "general"],
    "NotificationState": ["scheduled", "queued", "sent", "cancelled"],
    "AudienceType": ["all", "segment", "customer"],
    "LinkKind": ["none", "product"],
    "BannerLinkKind": ["none", "product", "category", "url"],
    "TemplateStatus": ["processing", "handed_to_courier", "delivered", "cancelled"],
    "DashboardRange": ["today", "week", "month", "custom"],
    "DashboardBucket": ["hour", "day"],
    "JobKind": ["billz_import"],
    "JobState": ["queued", "running", "succeeded", "failed"],
    "BillzImportMode": ["delta", "full"],
    "SessionRevokeReason": [
        "logout",
        "expired",
        "password_changed",
        "deactivated",
        "replaced",
        "admin_revoked",
        "refresh_reused",  # M2 (customer contract §2.1.6, admin §3.1)
    ],
    "LogoutScope": ["current", "others", "all"],
    "ExportFormat": ["csv", "xlsx"],
    "IntegrationMode": ["fake", "live"],
}
# Copied literally from docs/api/customer-api-v1.md section 3 (the enums marked new).
CUSTOMER_TABLE: dict[str, list[str]] = {
    "OtpPurpose": ["register", "login_new_device", "reset_password", "link_social"],
    "NextStep": ["verify_device", "verify_phone"],
    "CustomerSocialProvider": ["google", "facebook", "apple"],
    "SocialTokenType": ["id_token", "access_token"],
    "StockStatus": ["in_stock", "out_of_stock"],
    "CustomerProductOrdering": ["default", "price", "-price", "name"],
    "CartLineStatus": ["ok", "insufficient", "out_of_stock", "unavailable"],
    "OrderScope": ["active", "completed", "cancelled", "all"],
    "CustomerRefundState": ["in_progress", "completed"],
    "CashbackEarnedState": ["none", "credited", "reversed"],
    "TimelineType": [
        "order_created",
        "payment_confirmed",
        "payment_failed",
        "shipment_created",
        "handed_to_courier",
        "delivered",
        "cancelled",
        "refund_in_progress",
        "refund_succeeded",
    ],
    "ProgressStep": ["placed", "paid", "with_courier", "delivered", "picked_up", "cancelled"],
    "ProgressState": ["done", "current", "upcoming", "skipped"],
    "LedgerSource": ["online", "store", "admin"],
    "AppointmentScope": ["upcoming", "past"],
    "AppointmentCancelledBy": ["customer", "admin"],
    "InboxKind": ["promo", "general", "order_status", "appointment"],
    "InboxLinkKind": ["none", "product", "order", "appointment"],
    "PushDataType": ["order_status", "appointment", "campaign", "account_security", "session_replaced"],
    "AccountSecurityEvent": ["social_linked", "social_unlinked", "password_reset", "password_changed"],
}
ALL_TABLES = {**CONTRACT_TABLE, **CUSTOMER_TABLE}


def test_every_contract_enum_has_the_contract_literals() -> None:
    """Contract 3.1: "Enums: literals from §3, identical to the frontend"."""
    for name, literals in ALL_TABLES.items():
        enum = getattr(enums, name)
        assert issubclass(enum, TextChoices), name
        assert list(enum.values) == literals, name
    assert {enum.__name__ for enum in enums.CONTRACT_ENUMS} == set(ALL_TABLES)
    assert len(CONTRACT_TABLE["OrderEventType"]) == 16


def test_enum_name_overrides_point_at_values() -> None:
    """Each ``<Name>Enum`` component is pinned to the enum's literal list (``.values``)."""
    overrides = enums.enum_name_overrides()
    assert overrides["OrderStatusEnum"] == "glowderma.core.enums.OrderStatus.values"
    assert set(overrides) == {f"{name}Enum" for name in ALL_TABLES}
    assert len(set(overrides.values())) == len(overrides)


def test_enum_overrides_hash_like_the_schema_hook() -> None:
    """Regression: the hook hashes ``(value, value)`` pairs (hooks.py), so ``.values`` must match it and the
    label hack (``label == value``) is not needed; Django's default labels stay intact."""
    assert enums.OrderStatus.AWAITING_PAYMENT.label == "Awaiting Payment"
    assert enums.OrderStatus.choices[0] == ("awaiting_payment", "Awaiting Payment")
    for enum in enums.CONTRACT_ENUMS:
        field_side = list_hash([(value, value) for value in enum.values])
        assert field_side == list_hash([(value, value) for value in enum.values if value not in ("", None)])
        assert field_side != list_hash(list(enum.choices)), enum.__name__
    assert set(enums.enum_name_overrides()) <= set(spectacular_settings.ENUM_NAME_OVERRIDES)


def test_customer_enums_have_choice_sets_of_their_own() -> None:
    """Customer contract §3: drf-spectacular 0.30 refuses two override names for one choice set, so no new
    enum repeats another one's literals (the device platform reuses ``OrderChannel`` instead of a copy) and
    the staff ``SocialProviderEnum`` (google, facebook) stays distinct from ``CustomerSocialProviderEnum``."""
    choice_sets = [frozenset(enum.values) for enum in enums.CONTRACT_ENUMS]
    assert len(choice_sets) == len(set(choice_sets))
    assert not hasattr(enums, "DevicePlatform")
    overrides = spectacular_settings.ENUM_NAME_OVERRIDES
    assert overrides["SocialProviderEnum"] == ["google", "facebook"]
    assert overrides["CustomerSocialProviderEnum"] == "glowderma.core.enums.CustomerSocialProvider.values"
