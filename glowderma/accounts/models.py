import uuid
from datetime import datetime
from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from glowderma.accounts import media
from glowderma.core import enums, timeutils
from glowderma.core.models import TimeStampedModel, timestamp_default
from glowderma.core.search import SearchFold

from .managers import StaffUserManager

PHONE_E164_REGEX = r"^\+998\d{9}$"


def date_joined_default() -> datetime:
    return timeutils.now()


class StaffUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=10, choices=[("admin", "admin")], default="admin")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    # Admin contract §2.16: only a manager creates, edits, deactivates, reactivates staff or resets their
    # password. Every other staff right is the same for everyone (the client's single "admin" role).
    is_manager = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=date_joined_default)
    objects = StaffUserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="staffuser_email_ci_unique")]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)


class Customer(TimeStampedModel):
    """The loyalty identity of a person: one row per phone number for the lifetime of the business.

    Soft delete + reactivation (decision A1): the row is never deleted, ``phone`` is never reused.
    """

    phone = models.CharField(max_length=13, unique=True)  # E.164 "+998XXXXXXXXX"
    name = models.CharField(max_length=150, blank=True)  # "" after delete (scrubbed)
    language = models.CharField(
        max_length=2, choices=enums.ContentLanguage.choices, default=enums.ContentLanguage.UZ
    )
    status = models.CharField(
        max_length=8, choices=enums.CustomerStatus.choices, default=enums.CustomerStatus.ACTIVE, db_index=True
    )
    image = models.ImageField(upload_to=media.customer_image_path, blank=True, max_length=255)
    address_text = models.CharField(max_length=300, blank=True)  # denormalised default address
    registered_at = models.DateTimeField(default=timestamp_default, db_index=True)
    deleted_at = models.DateTimeField(null=True)
    reactivated_at = models.DateTimeField(null=True)  # last re-registration, if any
    last_login_at = models.DateTimeField(null=True)
    # M2 credentials (password login after OTP sign-up; never AbstractBaseUser — StaffUser is AUTH_USER_MODEL)
    password = models.CharField(max_length=128, blank=True)  # Django hasher format; "" = not set
    password_changed_at = models.DateTimeField(null=True)
    # M2 consent record (C3)
    terms_version = models.CharField(max_length=32, blank=True)
    consent_at = models.DateTimeField(null=True)
    consent_ip = models.GenericIPAddressField(null=True)
    # M2 history horizon (customer contract §2.2.3, M2-A15/A16): the customer's own orders, appointments and
    # inbox rows created before it belong to a previous holder of the number. Null = no horizon
    # (registered_at).
    history_from = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(phone__regex=PHONE_E164_REGEX), name="customer_phone_e164"),
            models.CheckConstraint(
                condition=(
                    Q(status=enums.CustomerStatus.ACTIVE, deleted_at__isnull=True)
                    | Q(status=enums.CustomerStatus.DELETED, deleted_at__isnull=False)
                ),
                name="customer_status_deleted_at_consistent",
            ),
            models.CheckConstraint(
                condition=Q(consent_at__isnull=True) | ~Q(terms_version=""),
                name="customer_consent_has_terms_version",
            ),
        ]
        indexes = [
            # Expression index so that fold(name) LIKE '%' || fold(term) || '%' is index-assisted (the admin
            # customers search, core.search / decision GC-1). Index.opclasses cannot be combined with an
            # expression; OpClass() is the expression form:
            # CREATE INDEX … USING gin (translate(lower("name"), …) gin_trgm_ops)
            GinIndex(OpClass(SearchFold("name"), name="gin_trgm_ops"), name="accounts_customer_fold_trgm"),
            models.Index(fields=["status", "registered_at"], name="accounts_customer_status_reg"),
        ]
        ordering = ["-registered_at", "-id"]

    def __str__(self) -> str:
        return f"{self.phone} {self.name}".strip()

    # DRF and core.idempotency treat request.user as a user object: a customer signed in through
    # CustomerJWTAuthentication is authenticated (no SQL behind either property).
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False


class Device(TimeStampedModel):
    """A mobile installation of one customer; carries the FCM token used by notifications."""

    class Platform(models.TextChoices):  # internal (same literals as enums.OrderChannel)
        IOS = "ios"
        ANDROID = "android"
        WEB = "web"  # a browser of the customer website (its BFF holds the device id in a cookie)

    customer = models.ForeignKey("accounts.Customer", on_delete=models.CASCADE, related_name="devices")
    fid = models.CharField(max_length=128)  # Firebase installation id
    platform = models.CharField(max_length=8, choices=Platform.choices)
    model = models.CharField(max_length=80, blank=True)  # "iPhone 14"
    os = models.CharField(max_length=40, blank=True)  # "iOS 18.1"
    app_version = models.CharField(max_length=20, blank=True)  # "1.0.0"
    locale = models.CharField(max_length=8, blank=True)  # device locale as reported, informational
    fcm_token = models.CharField(max_length=512, blank=True)
    push_enabled = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)  # false after logout/replace/delete
    last_active_at = models.DateTimeField(default=timestamp_default)
    fcm_invalidated_at = models.DateTimeField(null=True)  # set when FCM reports the token dead
    # M2 (customer contract §1.9): SHA-256 hex of the device key issued once on a verified sign-in; "" = none.
    key_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer", "fid"], name="device_customer_fid_unique"),
            models.UniqueConstraint(
                fields=["fcm_token"],
                condition=Q(is_active=True) & ~Q(fcm_token=""),
                name="device_active_fcm_token_unique",
            ),
            models.CheckConstraint(
                condition=Q(push_enabled=False) | ~Q(fcm_token=""), name="device_push_requires_token"
            ),
        ]
        indexes = [
            models.Index(
                fields=["customer", "is_active", "last_active_at"], name="accounts_device_cust_active"
            ),
            models.Index(
                fields=["push_enabled"],
                condition=Q(is_active=True, push_enabled=True),
                name="accounts_device_pushable",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.platform} {self.model or self.fid} of customer {self.customer_id}"


class Address(TimeStampedModel):
    """A saved delivery address of a customer; checkout snapshots its text into the order."""

    customer = models.ForeignKey("accounts.Customer", on_delete=models.CASCADE, related_name="addresses")
    label = models.CharField(max_length=60, blank=True)  # "Uy", "Ish"
    text = models.CharField(max_length=300)  # full human-readable address
    region_code = models.CharField(max_length=16, blank=True)  # BTS region id (M2 tariff/create)
    city_code = models.CharField(max_length=16, blank=True)  # BTS city id
    comment = models.CharField(max_length=200, blank=True)  # entrance, floor, intercom
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer"], condition=Q(is_default=True), name="address_one_default_per_customer"
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__isnull=False, longitude__isnull=False),
                name="address_latlng_pair",
            ),
        ]
        ordering = ["-is_default", "-updated_at"]

    def __str__(self) -> str:
        return f"{self.label or 'address'} #{self.pk} of customer {self.customer_id}"


class SmsCode(models.Model):
    """One OTP issuance for a phone and purpose, plus the single-use verification token it unlocks."""

    class Purpose(models.TextChoices):  # internal
        SIGNUP = "signup"
        RESET = "reset"
        NEW_DEVICE = "new_device"
        SOCIAL_LINK = "social_link"

    phone = models.CharField(max_length=13)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    code_hmac = models.CharField(max_length=64)  # HMAC-SHA256(code, SMS_CODE_HMAC_KEY), hex
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    created_at = models.DateTimeField(default=timestamp_default)
    expires_at = models.DateTimeField()  # created_at + 120 s
    sent_at = models.DateTimeField(null=True)  # claimed by the sms.send handler before the Eskiz call
    consumed_at = models.DateTimeField(null=True)  # verified successfully
    exhausted_at = models.DateTimeField(null=True)  # attempts reached max_attempts
    verification_token_hash = models.CharField(max_length=64, blank=True)  # SHA-256 of the issued token
    verification_expires_at = models.DateTimeField(null=True)  # consumed_at + 10 min
    verification_used_at = models.DateTimeField(null=True)  # single use
    request_ip = models.GenericIPAddressField(null=True)
    provider_message_id = models.CharField(max_length=64, blank=True)  # Eskiz message id (fake: "fake-<id>")

    class Meta:
        indexes = [
            models.Index(fields=["phone", "purpose", "created_at"], name="accounts_smscode_phone_purpose"),
            models.Index(fields=["expires_at"], name="accounts_smscode_expires"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(phone__regex=PHONE_E164_REGEX), name="smscode_phone_e164"),
            models.CheckConstraint(
                condition=Q(attempts__lte=models.F("max_attempts")), name="smscode_attempts_bound"
            ),
            models.UniqueConstraint(
                fields=["verification_token_hash"],
                condition=~Q(verification_token_hash=""),
                name="smscode_verification_token_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} code for {self.phone} ({self.created_at:%Y-%m-%d %H:%M})"


class CustomerSocialIdentity(models.Model):
    """Links a Google/Facebook identity to a customer (decision A6); separate from the staff table."""

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="social_identities"
    )
    provider = models.CharField(
        max_length=16, choices=[("google", "google"), ("facebook", "facebook"), ("apple", "apple")]
    )
    subject = models.CharField(max_length=254)
    email_at_link = models.EmailField(max_length=254, blank=True)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    last_login_at = models.DateTimeField(null=True)
    # Apple's refresh token sealed with core.secretbox (customer contract §2.1.8), revoked on unlink/deletion.
    provider_refresh_token = models.TextField(blank=True)
    # The OAuth client the refresh token was issued to (Apple: the ID token's ``aud`` bundle id, which is the
    # ``client_id`` of the code exchange and therefore of the revocation); "" = none or before 0007.
    provider_client_id = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "subject"], name="customer_social_provider_subject_unique"
            ),
            models.UniqueConstraint(
                fields=["customer", "provider"], name="customer_social_customer_provider_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}: {self.subject} → customer {self.customer_id}"


class AuthSession(models.Model):
    class AuthMethod(models.TextChoices):  # internal: how the session's sign-in was proven
        PASSWORD = "password"  # noqa: S105 - staff email + password, customer password (or SMS-verified)
        GOOGLE = "google"
        FACEBOOK = "facebook"
        APPLE = "apple"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audience = models.CharField(max_length=16, choices=[("staff", "staff"), ("customer", "customer")])
    staff = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="sessions"
    )
    customer = models.ForeignKey(
        "accounts.Customer", null=True, on_delete=models.PROTECT, related_name="sessions"
    )
    device = models.ForeignKey(
        "accounts.Device", null=True, on_delete=models.SET_NULL, related_name="sessions"
    )  # M2
    device_label = models.CharField(max_length=100)
    refresh_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=date_joined_default, editable=False)
    last_used_at = models.DateTimeField(default=date_joined_default)
    absolute_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    revoke_reason = models.CharField(
        max_length=24,
        blank=True,
        choices=[
            (value, value)
            for value in (
                "logout",
                "expired",
                "password_changed",
                "deactivated",
                "replaced",
                "admin_revoked",
                "refresh_reused",
            )
        ],
    )
    # M2 rotating refresh (customers only; staff refresh is non-rotating and leaves these empty)
    previous_refresh_hash = models.CharField(max_length=64, blank=True)
    rotated_at = models.DateTimeField(null=True)
    # Customer contract §2.1.6: the access-token claim ``rot`` is this counter (never a timestamp); the first
    # request carrying the current ``rot`` sets ``rotation_confirmed`` (CustomerJWTAuthentication).
    rotation = models.PositiveIntegerField(default=0)
    rotation_confirmed = models.BooleanField(default=False)
    # How the sign-in that opened this session was proven (admin contract §2.1: linking a staff identity
    # needs a password session). Kept on the row, so a customer refresh rotation carries it; "" = before 0007.
    auth_method = models.CharField(max_length=16, choices=AuthMethod.choices, blank=True)
    # B-W1 session management (customer contract §2.2.10): the client's user agent (the website's BFF
    # forwards the browser's as X-GD-Client-UA), the last client IP and the place a local GeoIP City database
    # resolves it to ("" = unknown / no database). Customer sessions only; refreshed by the throttled activity
    # bump (CustomerJWTAuthentication).
    user_agent = models.CharField(max_length=512, blank=True, default="", db_default="")
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    location = models.CharField(max_length=120, blank=True, default="", db_default="")

    class Meta:
        indexes = [
            models.Index(fields=["customer", "revoked_at"], name="accounts_authsession_cust_rev"),
            models.Index(fields=["staff", "revoked_at"], name="accounts_authsession_staff_rev"),
            models.Index(
                fields=["previous_refresh_hash"],
                condition=~Q(previous_refresh_hash=""),
                name="authsession_prev_refresh_idx",  # Django caps index names at 30
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(audience="staff", staff__isnull=False, customer__isnull=True)
                    | Q(audience="customer", customer__isnull=False, staff__isnull=True)
                ),
                name="authsession_subject_matches_audience",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.audience}: {self.pk}"


class SocialIdentity(models.Model):
    staff = models.ForeignKey(
        "accounts.StaffUser", on_delete=models.CASCADE, related_name="social_identities"
    )
    provider = models.CharField(max_length=16, choices=[("google", "google"), ("facebook", "facebook")])
    subject = models.CharField(max_length=254)
    email_at_link = models.EmailField(max_length=254, blank=True)
    created_at = models.DateTimeField(default=date_joined_default, editable=False)
    last_login_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "subject"], name="social_provider_subject_unique"),
            models.UniqueConstraint(fields=["staff", "provider"], name="social_staff_provider_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.provider}: {self.subject}"
