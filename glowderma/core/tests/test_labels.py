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
    "NOTIFICATION_STATE": {
        "scheduled": "Rejalashtirilgan",
        "queued": "Navbatda",
        "sent": "Yuborildi",
        "cancelled": "Bekor qilindi",
    },
    "NOTIFICATION_KIND": {"promo": "Aksiya", "general": "Umumiy"},
    "SPECIALIST_ROLE": {"dermatologist": "Dermatolog", "cosmetologist": "Kosmetolog"},
    "SERVICE_CATEGORY": {"dermatology": "Dermatologiya", "cosmetology": "Kosmetologiya"},
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
    "SPECIALIST_ROLE": enums.SpecialistRole,
    "SERVICE_CATEGORY": enums.ServiceCategory,
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


def test_active_label() -> None:
    """Contract 5.8: ``is_active`` of specialists and services "Faol" / "Nofaol"."""
    assert labels.active_label(True) == "Faol"
    assert labels.active_label(False) == "Nofaol"


def test_refund_state_line_by_reason_and_provider() -> None:
    """Contract §5.1 "Refund state": ``manual_required`` is never assumed to be Payme."""
    payme = "Payme kabinetida qaytarish kerak"
    click = "Mijozga qo‘lda to‘lang, so‘ng «Qaytarildi» deb belgilang"
    manual = enums.RefundState.MANUAL_REQUIRED
    assert labels.refund_state_label(manual, "payme", "payme") == payme
    assert labels.refund_state_label(manual, "payme", None) == payme  # no stored reason: by provider
    assert labels.refund_state_label(manual, "click", "no_paydoc") == click
    assert labels.refund_state_label(manual, "click", "fake_mode") == click
    assert labels.refund_state_label(manual, "click", None) == click
    expected = {
        "requested": "Qaytarish so‘raldi",
        "submitting": "Qaytarilmoqda",
        "succeeded": "Qaytarildi",
        "failed": "Qaytarib bo‘lmadi",
        "unknown": "Holat noma’lum · tekshiring",
    }
    for state, line in expected.items():
        assert labels.refund_state_label(state, "payme", "payme") == line
        assert labels.refund_state_label(state, "click", None) == line
    assert set(labels.REFUND_STATE) | {manual} == set(enums.RefundState.values)
