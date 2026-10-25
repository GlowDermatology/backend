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
