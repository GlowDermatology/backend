"""Models of `site_config` — design 01 §3 (slice B2): the typed settings singleton."""

from django.db import models
from django.db.models import Q

from glowderma.core.models import timestamp_default

PHONE_E164_REGEX = r"^\+998\d{9}$"


class SiteSettings(models.Model):
    """Typed, admin-editable business settings; one row forever (pk = 1, created by ``site_config.0002``)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    # contract §2.11
    reservation_minutes = models.PositiveSmallIntegerField(default=15)  # O3; 5..60
    support_phone = models.CharField(max_length=13, blank=True)  # "" ↔ null in the API
    pickup_address_uz = models.CharField(max_length=200, blank=True, verbose_name="olib ketish manzili (uz)")
    pickup_address_ru = models.CharField(max_length=200, blank=True, verbose_name="olib ketish manzili (ru)")
    pickup_address_en = models.CharField(max_length=200, blank=True, verbose_name="olib ketish manzili (en)")
    # Admin-panel settings screen (A1), M2 /settings/public
    cosmetologist_phone = models.CharField(max_length=13, blank=True)
    store_phone = models.CharField(max_length=13, blank=True)
    telegram_url = models.URLField(max_length=200, blank=True)
    instagram_url = models.URLField(max_length=200, blank=True)
    website_url = models.URLField(max_length=200, blank=True)
    # The PUBLISHED terms and privacy texts (GS-2): written only by ``services.publish_terms``, served by
    # ``settings/public/`` under ``terms_version`` and the copy a customer's consent refers to.
    privacy_policy_uz = models.TextField(blank=True, verbose_name="maxfiylik siyosati (uz)")
    privacy_policy_ru = models.TextField(blank=True, verbose_name="maxfiylik siyosati (ru)")
    privacy_policy_en = models.TextField(blank=True, verbose_name="maxfiylik siyosati (en)")
    terms_uz = models.TextField(blank=True, verbose_name="foydalanish shartlari (uz)")
    terms_ru = models.TextField(blank=True, verbose_name="foydalanish shartlari (ru)")
    terms_en = models.TextField(blank=True, verbose_name="foydalanish shartlari (en)")
    # The working copies staff edit with ``PATCH settings/`` (served by the admin API only).
    terms_draft_uz = models.TextField(blank=True, verbose_name="foydalanish shartlari (qoralama) (uz)")
    terms_draft_ru = models.TextField(blank=True, verbose_name="foydalanish shartlari (qoralama) (ru)")
    terms_draft_en = models.TextField(blank=True, verbose_name="foydalanish shartlari (qoralama) (en)")
    privacy_policy_draft_uz = models.TextField(blank=True, verbose_name="maxfiylik siyosati (qoralama) (uz)")
    privacy_policy_draft_ru = models.TextField(blank=True, verbose_name="maxfiylik siyosati (qoralama) (ru)")
    privacy_policy_draft_en = models.TextField(blank=True, verbose_name="maxfiylik siyosati (qoralama) (en)")
    # C3: stamped on Customer.consent
    terms_version = models.CharField(max_length=32, blank=True, verbose_name="shartlar versiyasi")
    terms_updated_at = models.DateTimeField(null=True)
    # P3 free-text cancellation/reschedule terms
    booking_policy_uz = models.TextField(blank=True, verbose_name="yozilish qoidalari (uz)")
    booking_policy_ru = models.TextField(blank=True, verbose_name="yozilish qoidalari (ru)")
    booking_policy_en = models.TextField(blank=True, verbose_name="yozilish qoidalari (en)")
    # bookkeeping
    updated_at = models.DateTimeField(default=timestamp_default, verbose_name="yangilangan")
    updated_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "sayt sozlamalari"
        verbose_name_plural = "sayt sozlamalari"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="sitesettings_singleton"),
            models.CheckConstraint(
                condition=Q(reservation_minutes__gte=5, reservation_minutes__lte=60),
                name="sitesettings_reservation_5_60",
            ),
            models.CheckConstraint(
                condition=Q(support_phone="") | Q(support_phone__regex=PHONE_E164_REGEX),
                name="sitesettings_support_phone_e164",
            ),
            models.CheckConstraint(
                condition=Q(cosmetologist_phone="") | Q(cosmetologist_phone__regex=PHONE_E164_REGEX),
                name="sitesettings_cosmetologist_phone_e164",
            ),
            models.CheckConstraint(
                condition=Q(store_phone="") | Q(store_phone__regex=PHONE_E164_REGEX),
                name="sitesettings_store_phone_e164",
            ),
        ]

    def __str__(self) -> str:
        return "Site settings"

    @classmethod
    def load(cls) -> SiteSettings:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
