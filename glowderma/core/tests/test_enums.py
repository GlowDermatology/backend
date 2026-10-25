from django.db.models import TextChoices

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
    "NotificationState": ["scheduled", "queued", "sent"],
    "AudienceType": ["all", "segment", "customer"],
    "LinkKind": ["none", "product"],
    "BannerLinkKind": ["none", "product", "category"],
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
    ],
    "LogoutScope": ["current", "others", "all"],
    "ExportFormat": ["csv", "xlsx"],
}


def test_every_contract_enum_has_the_contract_literals() -> None:
    """Contract 3.1: "Enums: literals from §3, identical to the frontend"."""
    for name, literals in CONTRACT_TABLE.items():
        enum = getattr(enums, name)
        assert issubclass(enum, TextChoices), name
        assert list(enum.values) == literals, name
    assert {enum.__name__ for enum in enums.CONTRACT_ENUMS} == set(CONTRACT_TABLE)
    assert len(CONTRACT_TABLE["OrderEventType"]) == 16


def test_labels_equal_values_so_choice_sets_hash_identically() -> None:
    for enum in enums.CONTRACT_ENUMS:
        assert enum.choices == [(value, value) for value in enum.values], enum.__name__
        assert all(member.label == member.value for member in enum), enum.__name__


def test_enum_name_overrides_point_at_choices() -> None:
    overrides = enums.enum_name_overrides()
    assert overrides["OrderStatusEnum"] == "glowderma.core.enums.OrderStatus.choices"
    assert set(overrides) == {f"{name}Enum" for name in CONTRACT_TABLE}
    assert len(set(overrides.values())) == len(overrides)
