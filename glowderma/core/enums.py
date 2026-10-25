"""Contract enumerations (admin API v1, section 3.1).

Every class is a Django ``TextChoices`` whose values are the contract literals, copied verbatim.
Class names are the contract enum names so that the OpenAPI component for each one is
``<Name>Enum`` (see :func:`enum_name_overrides`).
"""

from django.db.models import TextChoices


class ContractChoices(TextChoices):
    """A contract enum: the label *is* the literal.

    Human labels live in :mod:`glowderma.core.labels`; keeping ``label == value`` makes ``.choices``,
    ``.values`` and a plain literal list hash identically for ``ENUM_NAME_OVERRIDES``.
    """

    @property
    def label(self) -> str:
        return str(self.value)


class OrderStatus(ContractChoices):
    AWAITING_PAYMENT = "awaiting_payment"
    PROCESSING = "processing"
    HANDED_TO_COURIER = "handed_to_courier"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentStatus(ContractChoices):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentProvider(ContractChoices):
    CLICK = "click"
    PAYME = "payme"


class FulfilmentMethod(ContractChoices):
    DELIVERY = "delivery"
    PICKUP = "pickup"


class OrderChannel(ContractChoices):
    IOS = "ios"
    ANDROID = "android"


class OrderPeriod(ContractChoices):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"


class EventSource(ContractChoices):
    SYSTEM = "system"
    BTS = "bts"
    ADMIN = "admin"


class OrderEventType(ContractChoices):
    ORDER_CREATED = "order_created"
    RESERVATION_CREATED = "reservation_created"
    PAYMENT_CONFIRMED = "payment_confirmed"
    BILLZ_SALE_RECORDED = "billz_sale_recorded"
    CASHBACK_CREDITED = "cashback_credited"
    PAYMENT_FAILED = "payment_failed"
    SHIPMENT_CREATED = "shipment_created"
    SHIPMENT_FAILED = "shipment_failed"
    STATUS_CHANGED = "status_changed"
    HANDED_TO_COURIER = "handed_to_courier"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RESERVATION_EXPIRED = "reservation_expired"
    REFUND_REQUESTED = "refund_requested"
    REFUND_SUCCEEDED = "refund_succeeded"
    REFUND_FAILED = "refund_failed"


class CancellationReason(ContractChoices):
    RESERVATION_EXPIRED = "reservation_expired"
    ADMIN = "admin"
    CUSTOMER_REQUEST = "customer_request"


class ShipmentState(ContractChoices):
    NOT_REQUIRED = "not_required"
    NOT_CREATED = "not_created"
    FAILED = "failed"
    CREATED = "created"


class ShipmentVia(ContractChoices):
    WEBHOOK = "webhook"
    POLLING = "polling"


class RefundState(ContractChoices):
    REQUESTED = "requested"
    SUBMITTING = "submitting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"
    UNKNOWN = "unknown"


class CashbackHoldState(ContractChoices):
    ACTIVE = "active"
    CAPTURED = "captured"
    RELEASED = "released"


class OrderCashbackKind(ContractChoices):
    DEBIT = "debit"
    CREDIT = "credit"
    REVERSAL = "reversal"


class LedgerKind(ContractChoices):
    CREDIT = "credit"
    DEBIT = "debit"
    ADJUSTMENT = "adjustment"
    REVERSAL = "reversal"


class LedgerSourceType(ContractChoices):
    ORDER = "order"
    BILLZ_SALE = "billz_sale"
    ADMIN = "admin"


class AdjustmentDirection(ContractChoices):
    ADD = "add"
    SUBTRACT = "subtract"


class PurchaseSource(ContractChoices):
    ONLINE = "online"
    STORE_QR = "store_qr"


class RateBasis(ContractChoices):
    BEFORE_PURCHASE = "before_purchase"
    WITH_PURCHASE = "with_purchase"


class PolicyVersionState(ContractChoices):
    ACTIVE = "active"
    SCHEDULED = "scheduled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class TierWarning(ContractChoices):
    GAP = "gap"
    UNCOVERED_ABOVE = "uncovered_above"


class ProductStatus(ContractChoices):
    ACTIVE = "active"
    HIDDEN = "hidden"


class ProductChangeKind(ContractChoices):
    CREATED = "created"
    PRICE_CHANGED = "price_changed"
    ACTIVATED = "activated"
    HIDDEN = "hidden"
    PROMO_ADDED = "promo_added"
    PROMO_REMOVED = "promo_removed"
    UPDATED = "updated"


class ProductOrdering(ContractChoices):
    DEFAULT = "default"
    NAME = "name"
    NAME_DESC = "-name"


class ContentLanguage(ContractChoices):
    UZ = "uz"
    RU = "ru"
    EN = "en"


class CustomerStatus(ContractChoices):
    ACTIVE = "active"
    DELETED = "deleted"


class SpecialistRole(ContractChoices):
    DERMATOLOGIST = "dermatologist"
    COSMETOLOGIST = "cosmetologist"


class AppointmentStatus(ContractChoices):
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ATTENDED = "attended"
    # Not in the M1 admin contract: customers cancel their own appointments in M2.
    CANCELLED = "cancelled"


class AppointmentAction(ContractChoices):
    CONFIRM = "confirm"
    REJECT = "reject"
    ATTEND = "attend"


class RequestedVia(ContractChoices):
    APP = "app"
    ADMIN = "admin"


class BlockKind(ContractChoices):
    BUSY = "busy"
    OFF = "off"


class NotificationKind(ContractChoices):
    PROMO = "promo"
    GENERAL = "general"


class NotificationState(ContractChoices):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    SENT = "sent"


class AudienceType(ContractChoices):
    ALL = "all"
    SEGMENT = "segment"
    CUSTOMER = "customer"


class LinkKind(ContractChoices):
    NONE = "none"
    PRODUCT = "product"


class BannerLinkKind(ContractChoices):
    NONE = "none"
    PRODUCT = "product"
    CATEGORY = "category"


class TemplateStatus(ContractChoices):
    PROCESSING = "processing"
    HANDED_TO_COURIER = "handed_to_courier"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class DashboardRange(ContractChoices):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    CUSTOM = "custom"


class DashboardBucket(ContractChoices):
    HOUR = "hour"
    DAY = "day"


class JobKind(ContractChoices):
    BILLZ_IMPORT = "billz_import"


class JobState(ContractChoices):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BillzImportMode(ContractChoices):
    DELTA = "delta"
    FULL = "full"


class SessionRevokeReason(ContractChoices):
    LOGOUT = "logout"
    EXPIRED = "expired"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105
    DEACTIVATED = "deactivated"
    REPLACED = "replaced"
    ADMIN_REVOKED = "admin_revoked"


class LogoutScope(ContractChoices):
    CURRENT = "current"
    OTHERS = "others"
    ALL = "all"


class ExportFormat(ContractChoices):
    CSV = "csv"
    XLSX = "xlsx"


CONTRACT_ENUMS: tuple[type[ContractChoices], ...] = (
    OrderStatus,
    PaymentStatus,
    PaymentProvider,
    FulfilmentMethod,
    OrderChannel,
    OrderPeriod,
    EventSource,
    OrderEventType,
    CancellationReason,
    ShipmentState,
    ShipmentVia,
    RefundState,
    CashbackHoldState,
    OrderCashbackKind,
    LedgerKind,
    LedgerSourceType,
    AdjustmentDirection,
    PurchaseSource,
    RateBasis,
    PolicyVersionState,
    TierWarning,
    ProductStatus,
    ProductChangeKind,
    ProductOrdering,
    ContentLanguage,
    CustomerStatus,
    SpecialistRole,
    AppointmentStatus,
    AppointmentAction,
    RequestedVia,
    BlockKind,
    NotificationKind,
    NotificationState,
    AudienceType,
    LinkKind,
    BannerLinkKind,
    TemplateStatus,
    DashboardRange,
    DashboardBucket,
    JobKind,
    JobState,
    BillzImportMode,
    SessionRevokeReason,
    LogoutScope,
    ExportFormat,
)


def enum_name_overrides() -> dict[str, str]:
    """``SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`` entries: ``<Name>Enum`` -> dotted ``.choices`` path.

    drf-spectacular names a component after the field otherwise (``StatusEnum`` for every ``status``
    field), which collides as soon as two serializers use different status enums.
    """
    return {f"{enum.__name__}Enum": f"{__name__}.{enum.__name__}.choices" for enum in CONTRACT_ENUMS}
