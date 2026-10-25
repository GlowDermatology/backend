from glowderma.core import enums, labels

# Copied literally from docs/api/admin-api-v1.md section 5.8.
CONTRACT_LABELS: dict[str, dict[str, str]] = {
    "ORDER_STATUS": {
        "awaiting_payment": "To‘lov kutilmoqda",
        "processing": "Jarayonda",
        "handed_to_courier": "Kuryerga topshirildi",
        "delivered": "Yetkazildi",
        "cancelled": "Bekor qilindi",
    },
    "PAYMENT_STATUS": {
        "pending": "Kutilmoqda",
        "paid": "To‘langan",
        "failed": "Muvaffaqiyatsiz",
        "refunded": "Qaytarilgan",
    },
    "PROVIDER": {"click": "Click", "payme": "Payme"},
    "FULFILMENT": {"delivery": "Yetkazib berish", "pickup": "Olib ketish"},
    "CANCELLATION_REASON": {
        "reservation_expired": "Band muddati tugadi",
        "admin": "Admin bekor qildi",
        "customer_request": "Mijoz bekor qildi",
    },
    "PRODUCT_STATUS": {"active": "Faol", "hidden": "Yashirin"},
    "CUSTOMER_STATUS": {"active": "Faol", "deleted": "O‘chirilgan"},
    "LEDGER_KIND": {
        "credit": "Hisoblandi",
        "debit": "Ishlatildi",
        "adjustment": "Tuzatish",
        "reversal": "Qaytarildi",
    },
    "NOTIFICATION_STATE": {"scheduled": "Rejalashtirilgan", "queued": "Navbatda", "sent": "Yuborildi"},
    "NOTIFICATION_KIND": {"promo": "Aksiya", "general": "Umumiy"},
}
ENUM_FOR_LABELS = {
    "ORDER_STATUS": enums.OrderStatus,
    "PAYMENT_STATUS": enums.PaymentStatus,
    "PROVIDER": enums.PaymentProvider,
    "FULFILMENT": enums.FulfilmentMethod,
    "CANCELLATION_REASON": enums.CancellationReason,
    "PRODUCT_STATUS": enums.ProductStatus,
    "CUSTOMER_STATUS": enums.CustomerStatus,
    "LEDGER_KIND": enums.LedgerKind,
    "NOTIFICATION_STATE": enums.NotificationState,
    "NOTIFICATION_KIND": enums.NotificationKind,
}


def test_labels_match_contract_and_cover_every_enum_value() -> None:
    """Contract 5.8: Uzbek labels shared by the UI and exports."""
    for name, expected in CONTRACT_LABELS.items():
        mapping = getattr(labels, name)
        assert mapping == expected, name
        assert set(mapping) == set(ENUM_FOR_LABELS[name].values), name
        assert mapping["".join(next(iter(expected)))] == next(iter(expected.values()))


def test_yes_no() -> None:
    """Contract 5.8: Booleans in exports "Ha" / "Yo‘q"."""
    assert labels.yes_no(True) == "Ha"
    assert labels.yes_no(False) == "Yo‘q"
