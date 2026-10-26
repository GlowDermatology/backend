"""Contract enumerations (admin API v1, section 3.1).

Every class is a Django ``TextChoices`` whose values are the contract literals, copied verbatim.
Class names are the contract enum names so that the OpenAPI component for each one is
``<Name>Enum`` (see :func:`enum_name_overrides`). Human-facing Uzbek labels live in
:mod:`glowderma.core.labels`; Django's default labels are only used by ``get_<field>_display()``.
"""

from django.db.models import TextChoices


class OrderStatus(TextChoices):
    AWAITING_PAYMENT = "awaiting_payment"
    PROCESSING = "processing"
    HANDED_TO_COURIER = "handed_to_courier"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentStatus(TextChoices):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentProvider(TextChoices):
    CLICK = "click"
    PAYME = "payme"


class FulfilmentMethod(TextChoices):
    DELIVERY = "delivery"
    PICKUP = "pickup"


class OrderChannel(TextChoices):
    IOS = "ios"
    ANDROID = "android"


class OrderPeriod(TextChoices):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"


class EventSource(TextChoices):
    SYSTEM = "system"
    BTS = "bts"
    ADMIN = "admin"


class OrderEventType(TextChoices):
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


class CancellationReason(TextChoices):
    RESERVATION_EXPIRED = "reservation_expired"
    ADMIN = "admin"
    CUSTOMER_REQUEST = "customer_request"


class ShipmentState(TextChoices):
    NOT_REQUIRED = "not_required"
    NOT_CREATED = "not_created"
    FAILED = "failed"
    CREATED = "created"


class ShipmentVia(TextChoices):
    WEBHOOK = "webhook"
    POLLING = "polling"


class RefundState(TextChoices):
    REQUESTED = "requested"
    SUBMITTING = "submitting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"
    UNKNOWN = "unknown"


class CashbackHoldState(TextChoices):
    ACTIVE = "active"
    CAPTURED = "captured"
    RELEASED = "released"


class OrderCashbackKind(TextChoices):
    DEBIT = "debit"
    CREDIT = "credit"
    REVERSAL = "reversal"


class LedgerKind(TextChoices):
    CREDIT = "credit"
    DEBIT = "debit"
    ADJUSTMENT = "adjustment"
    REVERSAL = "reversal"


class LedgerSourceType(TextChoices):
    ORDER = "order"
    BILLZ_SALE = "billz_sale"
    ADMIN = "admin"


class AdjustmentDirection(TextChoices):
    ADD = "add"
    SUBTRACT = "subtract"


class PurchaseSource(TextChoices):
    ONLINE = "online"
    STORE_QR = "store_qr"


class RateBasis(TextChoices):
    BEFORE_PURCHASE = "before_purchase"
    WITH_PURCHASE = "with_purchase"


class PolicyVersionState(TextChoices):
    ACTIVE = "active"
    SCHEDULED = "scheduled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class TierWarning(TextChoices):
    GAP = "gap"
    UNCOVERED_ABOVE = "uncovered_above"


class ProductStatus(TextChoices):
    ACTIVE = "active"
    HIDDEN = "hidden"


class ProductChangeKind(TextChoices):
    CREATED = "created"
    PRICE_CHANGED = "price_changed"
    ACTIVATED = "activated"
    HIDDEN = "hidden"
    PROMO_ADDED = "promo_added"
    PROMO_REMOVED = "promo_removed"
    UPDATED = "updated"


class ProductOrdering(TextChoices):
    DEFAULT = "default"
    NAME = "name"
    NAME_DESC = "-name"


class ContentLanguage(TextChoices):
    UZ = "uz"
    RU = "ru"
    EN = "en"


class CustomerStatus(TextChoices):
    ACTIVE = "active"
    DELETED = "deleted"


class SpecialistRole(TextChoices):
    DERMATOLOGIST = "dermatologist"
    COSMETOLOGIST = "cosmetologist"


class AppointmentStatus(TextChoices):
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ATTENDED = "attended"
    # Not in the M1 admin contract: customers cancel their own appointments in M2.
    CANCELLED = "cancelled"


class AppointmentAction(TextChoices):
    CONFIRM = "confirm"
    REJECT = "reject"
    ATTEND = "attend"


class RequestedVia(TextChoices):
    APP = "app"
    ADMIN = "admin"


class BlockKind(TextChoices):
    BUSY = "busy"
    OFF = "off"


class NotificationKind(TextChoices):
    PROMO = "promo"
    GENERAL = "general"


class NotificationState(TextChoices):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    SENT = "sent"


class AudienceType(TextChoices):
    ALL = "all"
    SEGMENT = "segment"
    CUSTOMER = "customer"


class LinkKind(TextChoices):
    NONE = "none"
    PRODUCT = "product"


class BannerLinkKind(TextChoices):
    NONE = "none"
    PRODUCT = "product"
    CATEGORY = "category"


class TemplateStatus(TextChoices):
    PROCESSING = "processing"
    HANDED_TO_COURIER = "handed_to_courier"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class DashboardRange(TextChoices):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    CUSTOM = "custom"


class DashboardBucket(TextChoices):
    HOUR = "hour"
    DAY = "day"


class JobKind(TextChoices):
    BILLZ_IMPORT = "billz_import"


class JobState(TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BillzImportMode(TextChoices):
    DELTA = "delta"
    FULL = "full"


class SessionRevokeReason(TextChoices):
    LOGOUT = "logout"
    EXPIRED = "expired"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105
    DEACTIVATED = "deactivated"
    REPLACED = "replaced"
    ADMIN_REVOKED = "admin_revoked"


class LogoutScope(TextChoices):
    CURRENT = "current"
    OTHERS = "others"
    ALL = "all"


class ExportFormat(TextChoices):
    CSV = "csv"
    XLSX = "xlsx"


class IntegrationMode(TextChoices):
    """A provider adapter's mode (``*_MODE`` settings): ``settings/`` integrations, ``health/?ready=1``."""

    FAKE = "fake"
    LIVE = "live"


CONTRACT_ENUMS: tuple[type[TextChoices], ...] = (
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
    IntegrationMode,
)


def enum_name_overrides() -> dict[str, str]:
    """``SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`` entries: ``<Name>Enum`` -> dotted ``.values`` path.

    drf-spectacular names a component after the field otherwise (``StatusEnum`` for every ``status``
    field), which collides as soon as two serializers use different status enums. The override points
    at ``.values`` (a plain literal list) rather than ``.choices``: drf-spectacular 0.30 hashes
    ``(value, label)`` pairs and the field side hashes ``(value, value)``, so Django's title-case labels
    would never match.
    """
    return {f"{enum.__name__}Enum": f"{__name__}.{enum.__name__}.values" for enum in CONTRACT_ENUMS}
