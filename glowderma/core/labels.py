"""Uzbek labels shared by the admin UI and the exports (admin API v1, section 5.8).

Each mapping is keyed by the contract enum value (see :mod:`glowderma.core.enums`).
"""

from glowderma.core import enums

ORDER_STATUS: dict[str, str] = {
    enums.OrderStatus.AWAITING_PAYMENT: "To‘lov kutilmoqda",
    enums.OrderStatus.PROCESSING: "Jarayonda",
    enums.OrderStatus.HANDED_TO_COURIER: "Kuryerga topshirildi",
    enums.OrderStatus.DELIVERED: "Yetkazildi",
    enums.OrderStatus.CANCELLED: "Bekor qilindi",
}
PAYMENT_STATUS: dict[str, str] = {
    enums.PaymentStatus.PENDING: "Kutilmoqda",
    enums.PaymentStatus.PAID: "To‘langan",
    enums.PaymentStatus.FAILED: "Muvaffaqiyatsiz",
    enums.PaymentStatus.REFUNDED: "Qaytarilgan",
}
PROVIDER: dict[str, str] = {
    enums.PaymentProvider.CLICK: "Click",
    enums.PaymentProvider.PAYME: "Payme",
}
FULFILMENT: dict[str, str] = {
    enums.FulfilmentMethod.DELIVERY: "Yetkazib berish",
    enums.FulfilmentMethod.PICKUP: "Olib ketish",
}
CANCELLATION_REASON: dict[str, str] = {
    enums.CancellationReason.RESERVATION_EXPIRED: "Band muddati tugadi",
    enums.CancellationReason.ADMIN: "Admin bekor qildi",
    enums.CancellationReason.CUSTOMER_REQUEST: "Mijoz bekor qildi",
}
PRODUCT_STATUS: dict[str, str] = {
    enums.ProductStatus.ACTIVE: "Faol",
    enums.ProductStatus.HIDDEN: "Yashirin",
}
CUSTOMER_STATUS: dict[str, str] = {
    enums.CustomerStatus.ACTIVE: "Faol",
    enums.CustomerStatus.DELETED: "O‘chirilgan",
}
LEDGER_KIND: dict[str, str] = {
    enums.LedgerKind.CREDIT: "Hisoblandi",
    enums.LedgerKind.DEBIT: "Ishlatildi",
    enums.LedgerKind.ADJUSTMENT: "Tuzatish",
    enums.LedgerKind.REVERSAL: "Qaytarildi",
}
NOTIFICATION_STATE: dict[str, str] = {
    enums.NotificationState.SCHEDULED: "Rejalashtirilgan",
    enums.NotificationState.QUEUED: "Navbatda",
    enums.NotificationState.SENT: "Yuborildi",
    enums.NotificationState.CANCELLED: "Bekor qilindi",
}
NOTIFICATION_KIND: dict[str, str] = {
    enums.NotificationKind.PROMO: "Aksiya",
    enums.NotificationKind.GENERAL: "Umumiy",
}


def yes_no(value: bool) -> str:
    return "Ha" if value else "Yo‘q"


# Contract 1.8 export columns (Uzbek headers) of the catalog lists.
PRODUCT_EXPORT_HEADERS: tuple[str, ...] = (
    "ID",
    "Billz ID",
    "Billz nomi",
    "Nomi",
    "Kategoriya",
    "Narx (so‘m)",
    "Qoldiq",
    "Aksiya",
    "Holat",
    "Yangi",
)
CATEGORY_EXPORT_HEADERS: tuple[str, ...] = ("Slug", "Nomi", "Tartib", "Faol", "Mahsulotlar")
BANNER_EXPORT_HEADERS: tuple[str, ...] = (
    "ID",
    "Sarlavha",
    "Havola",
    "Faol",
    "Boshlanishi",
    "Tugashi",
    "Tartib",
)
# Contract 1.8 export columns (Uzbek headers) of the notifications, segments and audit-log lists (M1d).
NOTIFICATION_EXPORT_HEADERS: tuple[str, ...] = (
    "ID",
    "Sana",
    "Turi",
    "Sarlavha",
    "Auditoriya",
    "Holat",
    "Yuborildi",
    "Yetkazildi",
)
SEGMENT_EXPORT_HEADERS: tuple[str, ...] = ("ID", "Nomi", "A’zolar", "Push qurilmalar")
# The customer columns of ``mijozlar`` (contract 1.8), reused by the segment members export.
SEGMENT_MEMBER_EXPORT_HEADERS: tuple[str, ...] = (
    "ID",
    "Ism",
    "Telefon",
    "Ro‘yxatdan o‘tgan",
    "Jami xaridlar (so‘m)",
    "Cashback foizi",
    "Balans (so‘m)",
    "Holat",
)
# Contract 1.8 ``mijozlar`` (customers list, B4b): the same columns as the segment members export.
CUSTOMER_EXPORT_HEADERS: tuple[str, ...] = SEGMENT_MEMBER_EXPORT_HEADERS
AUDIT_LOG_EXPORT_HEADERS: tuple[str, ...] = (
    "ID",
    "Sana",
    "Xodim",
    "Amal",
    "Obyekt",
    "Obyekt ID",
    "IP",
    "So‘rov ID",
)
# Contract §5.1 "Refund state (new UI line)": also the ``Qaytarish`` column of the payments export (M1c).
# ``manual_required`` has no single line: :func:`refund_state_label` picks it by reason and provider.
REFUND_STATE: dict[str, str] = {
    enums.RefundState.REQUESTED: "Qaytarish so‘raldi",
    enums.RefundState.SUBMITTING: "Qaytarilmoqda",
    enums.RefundState.SUCCEEDED: "Qaytarildi",
    enums.RefundState.FAILED: "Qaytarib bo‘lmadi",
    enums.RefundState.UNKNOWN: "Holat noma’lum · tekshiring",
}
# Contract §5.1: a manual refund of Payme is a cabinet refund; a manual Click refund (no Click payment id, or
# Click fake in production) is paid to the customer by hand and then marked "Qaytarildi".
REFUND_MANUAL_PAYME = "Payme kabinetida qaytarish kerak"
REFUND_MANUAL_CLICK = "Mijozga qo‘lda to‘lang, so‘ng «Qaytarildi» deb belgilang"


def refund_state_label(state: str, provider: str, manual_reason: str | None = None) -> str:
    """The §5.1 refund-state line of ``state``; ``manual_required`` by its reason and provider — the Payme
    cabinet line only for a Payme refund (reason ``payme``), the Click line otherwise. Never assumes Payme."""
    if state != enums.RefundState.MANUAL_REQUIRED:
        return REFUND_STATE[state]
    if manual_reason == enums.RefundManualReason.PAYME or (
        not manual_reason and provider == enums.PaymentProvider.PAYME
    ):
        return REFUND_MANUAL_PAYME
    return REFUND_MANUAL_CLICK


# Contract 1.8 export columns (Uzbek headers) of the orders and payments lists (M1c).
ORDER_EXPORT_HEADERS: tuple[str, ...] = (
    "№",
    "Mijoz",
    "Telefon",
    "Sana",
    "Summa (so‘m)",
    "To‘lov",
    "Usul",
    "Olish usuli",
    "Holat",
    "BTS",
)
PAYMENT_EXPORT_HEADERS: tuple[str, ...] = (
    "ID",
    "Buyurtma №",
    "Mijoz",
    "Telefon",
    "Usul",
    "Holat",
    "Summa (so‘m)",
    "Tranzaksiya",
    "Yaratilgan",
    "Tasdiqlangan",
    "Qaytarish",
)
