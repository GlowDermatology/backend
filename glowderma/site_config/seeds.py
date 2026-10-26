"""Demo seeder of the site_config module (design 03 §7.4): the singleton's demo values.

* ``SEED_VALUES`` are (re)set on every run, as before.
* ``SEED_DEFAULTS`` — the demo contact numbers and links and short demo uz/ru/en legal texts (the terms and
  privacy policy as drafts) — only fill an empty column, so a run never overwrites texts or links staff
  entered on a demo or staging database.
* The terms are published once (``services.publish_terms`` at :data:`TERMS_PUBLISHED_AT`, actor system →
  version ``20241220-1``, copying the drafts into the published columns) when no version is published yet,
  and every active **seeded** customer (``accounts.seeds.all_customers``: same id and phone) without a consent
  accepts that version at their registration instant (they signed up under it), so the demo app is not asked
  to accept the terms again. Other customers (a real tester on staging) are never stamped. A later run
  changes nothing.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.accounts.seeds import all_customers
from glowderma.core import enums
from glowderma.core.seeding import SeedContext, pinned_clock, register_seeder
from glowderma.core.timeutils import TASHKENT
from glowderma.site_config import services
from glowderma.site_config.models import SiteSettings

SEED_VALUES = {
    "reservation_minutes": 15,
    "support_phone": "+998712000000",
    "pickup_address_uz": "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12",
    "pickup_address_ru": "",
    "pickup_address_en": "",
    "updated_by_id": None,
}
DEMO_NOTE = {
    "uz": "Namuna matn (demo): haqiqiy matnni Sozlamalar bo‘limida kiriting.",
    "ru": "Демонстрационный текст: настоящий текст вводится в разделе «Настройки».",
    "en": "Demo text: enter the real text under Settings.",
}
SEED_DEFAULTS = {
    "store_phone": "+998712000001",
    "cosmetologist_phone": "+998712000002",
    "telegram_url": "https://t.me/glowderma",
    "instagram_url": "https://instagram.com/glowderma",
    "website_url": "https://glowderma.uz",
    "terms_draft_uz": "Glow Derma ilovasidan foydalanish shartlari.\n\n" + DEMO_NOTE["uz"],
    "terms_draft_ru": "Условия использования приложения Glow Derma.\n\n" + DEMO_NOTE["ru"],
    "terms_draft_en": "Glow Derma app terms of use.\n\n" + DEMO_NOTE["en"],
    "privacy_policy_draft_uz": (
        "Maxfiylik siyosati: shaxsiy ma’lumotlaringiz O‘zbekiston hududida saqlanadi.\n\n" + DEMO_NOTE["uz"]
    ),
    "privacy_policy_draft_ru": (
        "Политика конфиденциальности: ваши персональные данные хранятся в Узбекистане.\n\n" + DEMO_NOTE["ru"]
    ),
    "privacy_policy_draft_en": "Privacy policy: your personal data is stored in Uzbekistan.\n\n"
    + DEMO_NOTE["en"],
    "booking_policy_uz": "Qabulni boshlanishidan oldin istalgan vaqtda bepul bekor qilishingiz mumkin.",
    "booking_policy_ru": "Запись можно бесплатно отменить в любое время до её начала.",
    "booking_policy_en": "You can cancel a booking free of charge at any time before it starts.",
}
TERMS_PUBLISHED_AT = datetime(2024, 12, 20, 10, 0, tzinfo=TASHKENT)  # before the first demo registration


@register_seeder("site_config", order=30)
@transaction.atomic
def seed_site_config(context: SeedContext) -> None:
    settings_row = SiteSettings.load()
    changes = {name: value for name, value in SEED_VALUES.items() if getattr(settings_row, name) != value}
    changes |= {name: value for name, value in SEED_DEFAULTS.items() if not getattr(settings_row, name)}
    if changes:
        SiteSettings.objects.filter(pk=settings_row.pk).update(**changes)
    if not SiteSettings.objects.get(pk=settings_row.pk).terms_version:
        with pinned_clock(TERMS_PUBLISHED_AT):
            services.publish_terms(actor=None)
    accept_seeded_terms()


def accept_seeded_terms() -> int:
    """Stamp the published version as the consent of every active seeded customer without one (matched by
    the seed's id **and** phone), at their registration (never before the publication); returns how many were
    stamped."""
    row = SiteSettings.objects.get(pk=1)
    if not row.terms_version or row.terms_updated_at is None:
        return 0
    seeded = {customer_id: phone for customer_id, _name, phone in all_customers()}
    customers = Customer.objects.filter(
        pk__in=seeded, status=enums.CustomerStatus.ACTIVE, terms_version=""
    ).order_by("pk")
    stamped = 0
    for customer_id, phone, registered_at in customers.values_list("pk", "phone", "registered_at"):
        if seeded[customer_id] != phone:
            continue
        with pinned_clock(max(registered_at, row.terms_updated_at)):
            account_services.stamp_consent(customer_id, terms_version=row.terms_version, ip="")
        stamped += 1
    return stamped
