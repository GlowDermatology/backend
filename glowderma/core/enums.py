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


class RefundManualReason(TextChoices):
    """Why a refund is ``manual_required`` (admin contract §2.2 ``Refund.manual_reason``, §4)."""

    PAYME = "payme"  # Payme refunds are cabinet refunds
    NO_PAYDOC = "no_paydoc"  # a Click payment without click_paydoc_id: Click cannot reverse it
    FAKE_MODE = "fake_mode"  # the Click port is fake under the production settings


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
    CANCELLED = "cancelled"  # M3: POST notifications/{id}/cancel/ of a scheduled campaign


class AudienceType(TextChoices):
    ALL = "all"
    SEGMENT = "segment"
    CUSTOMER = "customer"


class LinkKind(TextChoices):
    NONE = "none"
    PRODUCT = "product"
    URL = "url"  # gap ADM-10.1: an https link (Campaign.link_url, decision GC-3)


class BannerLinkKind(TextChoices):
    NONE = "none"
    PRODUCT = "product"
    CATEGORY = "category"
    URL = "url"  # gap X-BANNER-URL: an https link (Banner.link_url, decision GC-3)


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
    # M2 (customer contract §2.1.6): an old customer refresh token came back after the grace window.
    REFRESH_REUSED = "refresh_reused"


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


# --- Customer API (M2, customer contract §3) --------------------------------------------------------------
# Every class below has a choice set of its own: drf-spectacular 0.30 refuses two override names for one set
# (``--fail-on-warn``), which is why the device platform reuses ``OrderChannel`` instead of a second enum.


class OtpPurpose(TextChoices):
    REGISTER = "register"
    LOGIN_NEW_DEVICE = "login_new_device"
    RESET_PASSWORD = "reset_password"  # noqa: S105
    LINK_SOCIAL = "link_social"


class NextStep(TextChoices):
    VERIFY_DEVICE = "verify_device"
    VERIFY_PHONE = "verify_phone"


class CustomerSocialProvider(TextChoices):
    """Customer social sign-in providers; the staff routes keep their own ``SocialProviderEnum``."""

    GOOGLE = "google"
    FACEBOOK = "facebook"
    APPLE = "apple"


class SocialTokenType(TextChoices):
    ID_TOKEN = "id_token"  # noqa: S105
    ACCESS_TOKEN = "access_token"  # noqa: S105


class StockStatus(TextChoices):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"


class CustomerProductOrdering(TextChoices):
    DEFAULT = "default"
    PRICE = "price"
    PRICE_DESC = "-price"
    NAME = "name"


class CartLineStatus(TextChoices):
    OK = "ok"
    INSUFFICIENT = "insufficient"
    OUT_OF_STOCK = "out_of_stock"
    UNAVAILABLE = "unavailable"


class OrderScope(TextChoices):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ALL = "all"


class CustomerRefundState(TextChoices):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CashbackEarnedState(TextChoices):
    NONE = "none"
    CREDITED = "credited"
    REVERSED = "reversed"


class TimelineType(TextChoices):
    ORDER_CREATED = "order_created"
    PAYMENT_CONFIRMED = "payment_confirmed"
    PAYMENT_FAILED = "payment_failed"
    SHIPMENT_CREATED = "shipment_created"
    HANDED_TO_COURIER = "handed_to_courier"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUND_IN_PROGRESS = "refund_in_progress"
    REFUND_SUCCEEDED = "refund_succeeded"


class ProgressStep(TextChoices):
    PLACED = "placed"
    PAID = "paid"
    WITH_COURIER = "with_courier"
    DELIVERED = "delivered"
    PICKED_UP = "picked_up"
    CANCELLED = "cancelled"


class ProgressState(TextChoices):
    DONE = "done"
    CURRENT = "current"
    UPCOMING = "upcoming"
    SKIPPED = "skipped"


class LedgerSource(TextChoices):
    ONLINE = "online"
    STORE = "store"
    ADMIN = "admin"


class AppointmentScope(TextChoices):
    UPCOMING = "upcoming"
    PAST = "past"


class AppointmentCancelledBy(TextChoices):
    """The literals of ``appointments.Appointment.CancelledBy``."""

    CUSTOMER = "customer"
    ADMIN = "admin"


class InboxKind(TextChoices):
    """The literals of ``notifications.InboxNotification.Kind``."""

    PROMO = "promo"
    GENERAL = "general"
    ORDER_STATUS = "order_status"
    APPOINTMENT = "appointment"


class InboxLinkKind(TextChoices):
    NONE = "none"
    PRODUCT = "product"
    ORDER = "order"
    APPOINTMENT = "appointment"
    URL = "url"  # a campaign's https link (gap ADM-10.1)


class PushDataType(TextChoices):
    """FCM ``data.type`` (customer contract §2.14.2)."""

    ORDER_STATUS = "order_status"
    APPOINTMENT = "appointment"
    CAMPAIGN = "campaign"
    ACCOUNT_SECURITY = "account_security"
    SESSION_REPLACED = "session_replaced"


class AccountSecurityEvent(TextChoices):
    SOCIAL_LINKED = "social_linked"
    SOCIAL_UNLINKED = "social_unlinked"
    PASSWORD_RESET = "password_reset"  # noqa: S105
    PASSWORD_CHANGED = "password_changed"  # noqa: S105


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
    OtpPurpose,
    NextStep,
    CustomerSocialProvider,
    SocialTokenType,
    StockStatus,
    CustomerProductOrdering,
    CartLineStatus,
    OrderScope,
    CustomerRefundState,
    CashbackEarnedState,
    TimelineType,
    ProgressStep,
    ProgressState,
    LedgerSource,
    AppointmentScope,
    AppointmentCancelledBy,
    InboxKind,
    InboxLinkKind,
    PushDataType,
    AccountSecurityEvent,
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
