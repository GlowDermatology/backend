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
