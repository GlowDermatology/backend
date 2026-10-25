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
    pickup_address_uz = models.CharField(max_length=200, blank=True)
    pickup_address_ru = models.CharField(max_length=200, blank=True)
    pickup_address_en = models.CharField(max_length=200, blank=True)
    # Admin-panel settings screen (A1), M2 /settings/public
    cosmetologist_phone = models.CharField(max_length=13, blank=True)
    store_phone = models.CharField(max_length=13, blank=True)
    telegram_url = models.URLField(max_length=200, blank=True)
    instagram_url = models.URLField(max_length=200, blank=True)
    website_url = models.URLField(max_length=200, blank=True)
    privacy_policy_uz = models.TextField(blank=True)
    privacy_policy_ru = models.TextField(blank=True)
    privacy_policy_en = models.TextField(blank=True)
    terms_uz = models.TextField(blank=True)
    terms_ru = models.TextField(blank=True)
    terms_en = models.TextField(blank=True)
    terms_version = models.CharField(max_length=32, blank=True)  # C3: stamped on Customer.consent
    terms_updated_at = models.DateTimeField(null=True)
    booking_policy_uz = models.TextField(blank=True)  # P3 free-text cancellation/reschedule terms
    booking_policy_ru = models.TextField(blank=True)
    booking_policy_en = models.TextField(blank=True)
    # bookkeeping
    updated_at = models.DateTimeField(default=timestamp_default)
    updated_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "site settings"
        verbose_name_plural = "site settings"
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
