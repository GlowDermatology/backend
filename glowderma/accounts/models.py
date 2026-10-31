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
from glowderma.core.labels import masked_contact, stamp
from glowderma.core.models import TimeStampedModel, timestamp_default
from glowderma.core.search import SearchFold

from .managers import StaffUserManager

PHONE_E164_REGEX = r"^\+998\d{9}$"


def date_joined_default() -> datetime:
    return timeutils.now()


class StaffUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True, verbose_name="email")
    full_name = models.CharField(max_length=150, blank=True, verbose_name="to‘liq ism")
    role = models.CharField(max_length=10, choices=[("admin", "Admin")], default="admin", verbose_name="rol")
    is_active = models.BooleanField(default=True, verbose_name="faol")
    is_staff = models.BooleanField(default=False, verbose_name="konsolga kirish")
    # Admin contract §2.16: only a manager creates, edits, deactivates, reactivates staff or resets their
    # password. Every other staff right is the same for everyone (the client's single "admin" role).
    is_manager = models.BooleanField(default=False, verbose_name="menejer")
    date_joined = models.DateTimeField(default=date_joined_default, verbose_name="qo‘shilgan")
    objects = StaffUserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        verbose_name = "xodim"
        verbose_name_plural = "xodimlar"
        constraints = [models.UniqueConstraint(Lower("email"), name="staffuser_email_ci_unique")]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)


class Customer(TimeStampedModel):
    """The loyalty identity of a person: one row per phone number for the lifetime of the business.

    Soft delete + reactivation (decision A1): the row is never deleted, ``phone`` is never reused.

    Hybrid identity (customer contract §2.1, §2.2.10): an account may start without a phone — signed up by a
    verified e-mail or a social identity — and gets its phone (verified by SMS) at the first real action
    (checkout, pay, booking, the in-store QR answer 409 ``phone_required`` until then). A phone once attached
    is never removed. ``email`` is stored normalised (trimmed, lower case) and only once verified by a code;
    deletion scrubs it (the phone stays, A1).
    """

    # E.164 "+998XXXXXXXXX"
    phone = models.CharField(max_length=13, unique=True, null=True, blank=True, verbose_name="telefon")
    # normalised, verified
    email = models.CharField(max_length=254, unique=True, null=True, blank=True, verbose_name="email")
    email_verified_at = models.DateTimeField(null=True, verbose_name="email tasdiqlangan")
    name = models.CharField(max_length=150, blank=True, verbose_name="nomi")  # "" after delete (scrubbed)
    language = models.CharField(
        max_length=2,
        choices=enums.ContentLanguage.choices,
        default=enums.ContentLanguage.UZ,
        verbose_name="til",
    )
    status = models.CharField(
        max_length=8,
        choices=enums.CustomerStatus.choices,
        default=enums.CustomerStatus.ACTIVE,
        db_index=True,
        verbose_name="holat",
    )
    image = models.ImageField(
        upload_to=media.customer_image_path, blank=True, max_length=255, verbose_name="rasm"
    )
    # denormalised default address
    address_text = models.CharField(max_length=300, blank=True, verbose_name="manzil")
    registered_at = models.DateTimeField(
        default=timestamp_default, db_index=True, verbose_name="ro‘yxatdan o‘tgan"
    )
    deleted_at = models.DateTimeField(null=True, verbose_name="o‘chirilgan")
    # last re-registration, if any
    reactivated_at = models.DateTimeField(null=True, verbose_name="qayta faollashgan")
    last_login_at = models.DateTimeField(null=True, verbose_name="oxirgi kirish")
    # M2 credentials (password login after OTP sign-up; never AbstractBaseUser — StaffUser is AUTH_USER_MODEL)
    password = models.CharField(
        max_length=128, blank=True, verbose_name="parol"
    )  # Django hasher format; "" = not set
    password_changed_at = models.DateTimeField(null=True, verbose_name="parol o‘zgargan")
    # M2 consent record (C3)
    terms_version = models.CharField(max_length=32, blank=True, verbose_name="shartlar versiyasi")
    consent_at = models.DateTimeField(null=True, verbose_name="rozilik vaqti")
    consent_ip = models.GenericIPAddressField(null=True, verbose_name="rozilik IP manzili")
    # M2 history horizon (customer contract §2.2.3, M2-A15/A16): the customer's own orders, appointments and
    # inbox rows created before it belong to a previous holder of the number. Null = no horizon
    # (registered_at).
    history_from = models.DateTimeField(null=True, verbose_name="tarix boshlanishi")

    class Meta:
        verbose_name = "mijoz"
        verbose_name_plural = "mijozlar"
        constraints = [
            models.CheckConstraint(condition=Q(phone__regex=PHONE_E164_REGEX), name="customer_phone_e164"),
            models.CheckConstraint(
                condition=Q(email__isnull=True)
                | (
                    Q(email=Lower("email"), email__contains="@", email_verified_at__isnull=False)
                    & ~Q(email__regex=r"\s")
                ),
                name="customer_email_normalised_verified",
            ),
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
        # «Malika · +998 90 *** ** 67»: the contact masked (customer contract §1.4); in full on its page only
        contact = masked_contact(self.phone, self.email)
        return " · ".join(part for part in (self.name, contact) if part) or f"Mijoz #{self.pk}"

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
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"
        WEB = "web", "Veb-sayt"  # a browser of the customer website (its BFF holds the device id in a cookie)

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="devices", verbose_name="mijoz"
    )
    fid = models.CharField(max_length=128, verbose_name="FID")  # Firebase installation id
    platform = models.CharField(max_length=8, choices=Platform.choices, verbose_name="platforma")
    model = models.CharField(max_length=80, blank=True, verbose_name="model")  # "iPhone 14"
    os = models.CharField(max_length=40, blank=True, verbose_name="OT")  # "iOS 18.1"
    app_version = models.CharField(max_length=20, blank=True, verbose_name="ilova versiyasi")  # "1.0.0"
    locale = models.CharField(
        max_length=8, blank=True, verbose_name="til"
    )  # device locale as reported, informational
    fcm_token = models.CharField(max_length=512, blank=True, verbose_name="FCM token")
    push_enabled = models.BooleanField(default=False, verbose_name="push yoqilgan")
    is_active = models.BooleanField(default=True, verbose_name="faol")  # false after logout/replace/delete
    last_active_at = models.DateTimeField(default=timestamp_default, verbose_name="oxirgi faollik")
    fcm_invalidated_at = models.DateTimeField(
        null=True, verbose_name="FCM token bekor qilingan"
    )  # set when FCM reports the token dead
    # M2 (customer contract §1.9): SHA-256 hex of the device key issued once on a verified sign-in; "" = none.
    key_hash = models.CharField(max_length=64, blank=True, verbose_name="kalit xeshi")

    class Meta:
        verbose_name = "qurilma"
        verbose_name_plural = "qurilmalar"
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
        return f"{self.get_platform_display()} {self.model or self.fid} · mijoz #{self.customer_id}"


class Address(TimeStampedModel):
    """A saved delivery address of a customer; checkout snapshots its text into the order."""

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="addresses", verbose_name="mijoz"
    )
    label = models.CharField(max_length=60, blank=True, verbose_name="yorliq")  # "Uy", "Ish"
    text = models.CharField(max_length=300, verbose_name="matn")  # full human-readable address
    # BTS region id (M2 tariff/create)
    region_code = models.CharField(max_length=16, blank=True, verbose_name="viloyat kodi")
    city_code = models.CharField(max_length=16, blank=True, verbose_name="shahar kodi")  # BTS city id
    comment = models.CharField(max_length=200, blank=True, verbose_name="izoh")  # entrance, floor, intercom
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, verbose_name="kenglik")
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, verbose_name="uzunlik")
    is_default = models.BooleanField(default=False, verbose_name="asosiy")

    class Meta:
        verbose_name = "manzil"
        verbose_name_plural = "manzillar"
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
        return f"{self.label or 'Manzil'} #{self.pk} · mijoz #{self.customer_id}"


class SmsCode(models.Model):
    """One OTP issuance for a phone **or an e-mail address** (hybrid identity; the historical name stays) and
    purpose, plus the single-use verification token it unlocks. Exactly one of ``phone`` / ``email`` is set:
    the code went out by SMS or by e-mail, with the same engine, limits and HMAC at rest."""

    class Purpose(models.TextChoices):  # internal
        SIGNUP = "signup", "Ro‘yxatdan o‘tish"
        RESET = "reset", "Parolni tiklash"
        NEW_DEVICE = "new_device", "Yangi qurilma"
        SOCIAL_LINK = "social_link", "Ijtimoiy tarmoqni bog‘lash"
        VERIFY_EMAIL = (
            "verify_email",
            "E-pochtani tasdiqlash",
        )  # attach an e-mail to a signed-in account (e-mail only)
        ATTACH_PHONE = (
            "attach_phone",
            "Telefonni bog‘lash",
        )  # attach a phone to a signed-in account (SMS only)

    phone = models.CharField(max_length=13, blank=True, verbose_name="telefon")
    # normalised (lower case); db_default: a writer of the previous release (deploy window) inserts SMS codes
    # without the column
    email = models.CharField(max_length=254, blank=True, default="", db_default="", verbose_name="email")
    purpose = models.CharField(max_length=20, choices=Purpose.choices, verbose_name="maqsad")
    code_hmac = models.CharField(
        max_length=64, verbose_name="kod HMAC"
    )  # HMAC-SHA256(code, SMS_CODE_HMAC_KEY), hex
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="urinishlar")
    max_attempts = models.PositiveSmallIntegerField(default=5, verbose_name="maks. urinishlar")
    created_at = models.DateTimeField(default=timestamp_default, verbose_name="yaratilgan")
    expires_at = models.DateTimeField(verbose_name="amal qilish muddati")  # created_at + 120 s
    # claimed by the sms.send handler before the Eskiz call
    sent_at = models.DateTimeField(null=True, verbose_name="yuborilgan")
    consumed_at = models.DateTimeField(null=True, verbose_name="ishlatilgan")  # verified successfully
    # attempts reached max_attempts
    exhausted_at = models.DateTimeField(null=True, verbose_name="tugagan urinishlar vaqti")
    verification_token_hash = models.CharField(
        max_length=64, blank=True, verbose_name="tasdiq tokeni xeshi"
    )  # SHA-256 of the issued token
    verification_expires_at = models.DateTimeField(
        null=True, verbose_name="tasdiq muddati"
    )  # consumed_at + 10 min
    verification_used_at = models.DateTimeField(null=True, verbose_name="tasdiq ishlatilgan")  # single use
    request_ip = models.GenericIPAddressField(null=True, verbose_name="so‘rov IP manzili")
    provider_message_id = models.CharField(
        max_length=64, blank=True, verbose_name="provayder xabar ID"
    )  # Eskiz message id (fake: "fake-<id>")

    class Meta:
        verbose_name = "SMS kod"
        verbose_name_plural = "SMS kodlar"
        indexes = [
            models.Index(fields=["phone", "purpose", "created_at"], name="accounts_smscode_phone_purpose"),
            models.Index(
                fields=["email", "purpose", "created_at"],
                condition=~Q(email=""),
                name="accounts_smscode_email_purpose",
            ),
            models.Index(fields=["expires_at"], name="accounts_smscode_expires"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(phone__regex=PHONE_E164_REGEX) & Q(email=""))
                | (Q(phone="") & Q(email__contains="@")),
                name="smscode_one_target",
            ),
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
        contact = masked_contact(self.phone, self.email)
        return f"{self.get_purpose_display()} kodi · {contact} · {stamp(self.created_at)}"


class CustomerSocialIdentity(models.Model):
    """Links a Google/Facebook identity to a customer (decision A6); separate from the staff table."""

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="social_identities", verbose_name="mijoz"
    )
    provider = models.CharField(
        max_length=16,
        choices=[("google", "Google"), ("facebook", "Facebook"), ("apple", "Apple")],
        verbose_name="provayder",
    )
    subject = models.CharField(max_length=254, verbose_name="subyekt")
    email_at_link = models.EmailField(max_length=254, blank=True, verbose_name="bog‘langandagi email")
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")
    last_login_at = models.DateTimeField(null=True, verbose_name="oxirgi kirish")
    # Apple's refresh token sealed with core.secretbox (customer contract §2.1.8), revoked on unlink/deletion.
    provider_refresh_token = models.TextField(blank=True, verbose_name="provayder refresh tokeni")
    # The OAuth client the refresh token was issued to (Apple: the ID token's ``aud`` bundle id, which is the
    # ``client_id`` of the code exchange and therefore of the revocation); "" = none or before 0007.
    provider_client_id = models.CharField(max_length=255, blank=True, verbose_name="provayder ilova ID")

    class Meta:
        verbose_name = "ijtimoiy kirish"
        verbose_name_plural = "ijtimoiy kirishlar"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "subject"], name="customer_social_provider_subject_unique"
            ),
            models.UniqueConstraint(
                fields=["customer", "provider"], name="customer_social_customer_provider_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_provider_display()}: {self.subject} → mijoz #{self.customer_id}"


class AuthSession(models.Model):
    class AuthMethod(models.TextChoices):  # internal: how the session's sign-in was proven
        PASSWORD = "password", "Parol"  # noqa: S105 - staff email + password, customer password (or SMS-verified)
        GOOGLE = "google", "Google"
        FACEBOOK = "facebook", "Facebook"
        APPLE = "apple", "Apple"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audience = models.CharField(
        max_length=16, choices=[("staff", "Xodim"), ("customer", "Mijoz")], verbose_name="auditoriya"
    )
    staff = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="sessions",
        verbose_name="xodim",
    )
    customer = models.ForeignKey(
        "accounts.Customer",
        null=True,
        on_delete=models.PROTECT,
        related_name="sessions",
        verbose_name="mijoz",
    )
    # M2
    device = models.ForeignKey(
        "accounts.Device",
        null=True,
        on_delete=models.SET_NULL,
        related_name="sessions",
        verbose_name="qurilma",
    )
    device_label = models.CharField(max_length=100, verbose_name="qurilma nomi")
    refresh_hash = models.CharField(max_length=64, unique=True, verbose_name="refresh token xeshi")
    created_at = models.DateTimeField(default=date_joined_default, editable=False, verbose_name="yaratilgan")
    last_used_at = models.DateTimeField(default=date_joined_default, verbose_name="oxirgi foydalanish")
    absolute_expires_at = models.DateTimeField(verbose_name="mutlaq muddat")
    revoked_at = models.DateTimeField(null=True, verbose_name="tugatilgan")
    revoke_reason = models.CharField(
        max_length=24,
        blank=True,
        choices=[
            ("logout", "Chiqish"),
            ("expired", "Muddati tugadi"),
            ("password_changed", "Parol o‘zgartirildi"),
            ("deactivated", "Hisob faolsizlantirildi"),
            ("replaced", "Yangi sessiya bilan almashtirildi"),
            ("admin_revoked", "Admin tugatdi"),
            ("refresh_reused", "Eski token qayta ishlatildi"),
        ],
        verbose_name="tugatish sababi",
    )
    # M2 rotating refresh (customers only; staff refresh is non-rotating and leaves these empty)
    previous_refresh_hash = models.CharField(
        max_length=64, blank=True, verbose_name="oldingi refresh token xeshi"
    )
    rotated_at = models.DateTimeField(null=True, verbose_name="token yangilangan")
    # Customer contract §2.1.6: the access-token claim ``rot`` is this counter (never a timestamp); the first
    # request carrying the current ``rot`` sets ``rotation_confirmed`` (CustomerJWTAuthentication).
    rotation = models.PositiveIntegerField(default=0, verbose_name="token yangilanishlari soni")
    rotation_confirmed = models.BooleanField(default=False, verbose_name="yangilanish tasdiqlangan")
    # How the sign-in that opened this session was proven (admin contract §2.1: linking a staff identity
    # needs a password session). Kept on the row, so a customer refresh rotation carries it; "" = before 0007.
    auth_method = models.CharField(
        max_length=16, choices=AuthMethod.choices, blank=True, verbose_name="kirish usuli"
    )
    # B-W1 session management (customer contract §2.2.10): the client's user agent (the website's BFF
    # forwards the browser's as X-GD-Client-UA), the last client IP and the place a local GeoIP City database
    # resolves it to ("" = unknown / no database). Customer sessions only; refreshed by the throttled activity
    # bump (CustomerJWTAuthentication).
    user_agent = models.CharField(
        max_length=512, blank=True, default="", db_default="", verbose_name="user-agent"
    )
    last_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="oxirgi IP")
    location = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="joylashuv"
    )

    class Meta:
        verbose_name = "sessiya"
        verbose_name_plural = "sessiyalar"
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
        return f"{self.get_audience_display()} sessiyasi {self.pk}"


class SocialIdentity(models.Model):
    staff = models.ForeignKey(
        "accounts.StaffUser", on_delete=models.CASCADE, related_name="social_identities", verbose_name="xodim"
    )
    provider = models.CharField(
        max_length=16, choices=[("google", "Google"), ("facebook", "Facebook")], verbose_name="provayder"
    )
    subject = models.CharField(max_length=254, verbose_name="subyekt")
    email_at_link = models.EmailField(max_length=254, blank=True, verbose_name="bog‘langandagi email")
    created_at = models.DateTimeField(default=date_joined_default, editable=False, verbose_name="yaratilgan")
    last_login_at = models.DateTimeField(null=True, verbose_name="oxirgi kirish")

    class Meta:
        verbose_name = "xodim ijtimoiy kirishi"
        verbose_name_plural = "xodim ijtimoiy kirishlari"
        constraints = [
            models.UniqueConstraint(fields=["provider", "subject"], name="social_provider_subject_unique"),
            models.UniqueConstraint(fields=["staff", "provider"], name="social_staff_provider_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.get_provider_display()}: {self.subject}"
