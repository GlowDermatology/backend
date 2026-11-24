"""One-time codes sent by SMS or e-mail for sign-in, verification and recovery."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext

from glowderma.accounts.models.defaults import PHONE_E164_REGEX
from glowderma.core.labels import masked_contact, stamp
from glowderma.core.models import timestamp_default


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
    # claimed by the sms.send handler before the provider call
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
    )  # provider message id: DevSMS (earlier rows: Eskiz), "fake-<id>" for the fake

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
        return gettext("%(purpose)s kodi · %(contact)s · %(created)s") % {
            "purpose": self.get_purpose_display(),
            "contact": contact,
            "created": stamp(self.created_at),
        }


__all__ = ["SmsCode"]
