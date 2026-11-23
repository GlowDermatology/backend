"""Customer identity: the customer row, saved delivery addresses and linked social identities."""

from __future__ import annotations

from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext

from glowderma.accounts import media
from glowderma.accounts.models.defaults import PHONE_E164_REGEX
from glowderma.core import enums
from glowderma.core.labels import masked_contact
from glowderma.core.models import TimeStampedModel, timestamp_default
from glowderma.core.search import SearchFold


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
        named = " · ".join(part for part in (self.name, contact) if part)
        return named or gettext("Mijoz #%(id)s") % {"id": self.pk}

    # DRF and core.idempotency treat request.user as a user object: a customer signed in through
    # CustomerJWTAuthentication is authenticated (no SQL behind either property).
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False


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
        return gettext("%(label)s #%(id)s · mijoz #%(customer)s") % {
            "label": self.label or gettext("Manzil"),
            "id": self.pk,
            "customer": self.customer_id,
        }


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
        return gettext("%(provider)s: %(subject)s → mijoz #%(customer)s") % {
            "provider": self.get_provider_display(),
            "subject": self.subject,
            "customer": self.customer_id,
        }


__all__ = ["Address", "Customer", "CustomerSocialIdentity"]
