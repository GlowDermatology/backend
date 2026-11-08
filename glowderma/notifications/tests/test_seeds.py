"""notifications.seed (design 03 §7.12): segments, Malika's memberships, the five campaigns (campaign 4
approved with its confirmed test on Malika's device, campaign 5 a draft); idempotent, standalone-safe, every
timestamp pinned."""

import importlib
from datetime import datetime, timedelta

import pytest
from django.db import models

from glowderma.accounts import seeds as accounts_seeds
from glowderma.accounts.models import Customer, StaffUser
from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core import enums, seeding
from glowderma.core.seeding import SeedContext
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications import seeds
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CampaignTest,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
    TestDevice,
)
from glowderma.notifications.seeds import seed_notifications

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)

pytestmark = pytest.mark.django_db


def dt(month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TASHKENT)


def counts() -> tuple[int, ...]:
    return (
        CustomerSegment.objects.count(),
        SegmentMember.objects.count(),
        Campaign.objects.count(),
        CampaignRecipient.objects.count(),
        InboxNotification.objects.count(),
        StatusTemplate.objects.count(),
        TestDevice.objects.count(),
        CampaignTest.objects.count(),
    )


def test_seeder_registered_with_order_90(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})
    importlib.reload(seeds)
    assert [(name, order) for name, order, _ in seeding.registered_seeders()] == [("notifications", 90)]


def test_seed_reproduces_the_design_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    accounts_seeds.seed_accounts(CONTEXT)
    product = ProductFactory.create(
        billz_id=seeds.LINK_PRODUCT_BILLZ_ID, name_uz="Seramidli namlantiruvchi krem"
    )
    admin = StaffUser.objects.get(email=seeds.SEED_ADMIN_EMAIL)
    assert admin.is_superuser  # the demo administrator tests, approves and stops campaigns
    seed_notifications(CONTEXT)
    assert counts() == (2, 2, 5, 0, 0, 4, 1, 1)
    seed_notifications(CONTEXT)
    assert counts() == (2, 2, 5, 0, 0, 4, 1, 1)

    regular = CustomerSegment.objects.get(pk=1)
    big = CustomerSegment.objects.get(pk=2)
    assert (regular.name, regular.min_online_orders, regular.min_lifetime_amount) == (
        "Doimiy mijozlar",
        3,
        None,
    )
    assert (big.name, big.min_online_orders, big.min_lifetime_amount) == ("10 mln+ xarid", None, 10_000_000)
    assert regular.created_by == admin and regular.created_at == regular.updated_at == dt(10, 10, 10, 0)
    malika = Customer.objects.get(pk=5)
    assert set(SegmentMember.objects.values_list("segment_id", "customer_id")) == {(1, 5), (2, 5)}
    member = SegmentMember.objects.get(segment=regular)
    assert member.customer == malika and member.added_by == admin and member.added_at == dt(10, 10, 10, 0)

    rows = {row.pk: row for row in Campaign.objects.all()}
    assert sorted(rows) == [1, 2, 3, 4, 5]
    assert list(Campaign.objects.values_list("pk", flat=True)) == [
        4,
        5,
        3,
        2,
        1,
    ]  # coalesce(scheduled, created) desc
    one = rows[1]
    assert (one.kind, one.title, one.audience_type, one.audience_segment, one.audience_segment_name) == (
        "promo", "Quyoshdan himoya: 15% chegirma", "segment", big, "10 mln+ xarid",
    )  # fmt: skip
    assert (one.created_at, one.scheduled_at, one.started_at, one.sent_at) == (
        dt(10, 18, 16, 30), None, dt(10, 18, 16, 29), dt(10, 18, 16, 31),
    )  # fmt: skip
    assert (one.recipient_count, one.sent, one.failed, one.delivered, one.state) == (318, 318, 0, 301, "sent")
    assert one.updated_at == one.sent_at and one.author == admin and one.link_kind == "none"
    two = rows[2]
    assert (two.kind, two.title, two.audience_type) == ("general", "Yangi mahsulotlar keldi", "all")
    assert (two.created_at, two.scheduled_at, two.started_at, two.sent_at) == (
        dt(10, 20, 15, 0), dt(10, 21, 10, 0), dt(10, 21, 10, 0), dt(10, 21, 10, 2),
    )  # fmt: skip
    assert (two.recipient_count, two.sent, two.failed, two.delivered) == (4740, 4740, 0, 4205)
    three = rows[3]
    assert (three.kind, three.title, three.audience_type) == ("promo", "Kuzgi namlik to‘plami", "all")
    assert three.body == "Tanlangan namlantiruvchi kremlarga 20% chegirma — 30-noyabrgacha."
    assert (three.link_kind, three.link_product, three.link_product_label) == (
        "product", product, "Seramidli namlantiruvchi krem",
    )  # fmt: skip
    assert (three.created_at, three.scheduled_at, three.started_at, three.sent_at) == (
        dt(10, 24, 9, 40), dt(10, 24, 12, 0), dt(10, 24, 12, 0), dt(10, 24, 12, 2),
    )  # fmt: skip
    assert (three.recipient_count, three.sent, three.failed, three.delivered) == (4751, 4751, 0, 4388)
    four = rows[4]
    assert (four.kind, four.title, four.audience_type, four.audience_segment, four.audience_segment_name) == (
        "general", "Yangi dermatolog qabuli ochildi", "segment", regular, "Doimiy mijozlar",
    )  # fmt: skip
    assert (four.state, four.created_at, four.scheduled_at, four.started_at, four.sent_at) == (
        "scheduled", dt(10, 24, 11, 30), dt(10, 26, 10, 0), None, None,
    )  # fmt: skip
    assert (four.recipient_count, four.sent, four.failed, four.delivered) == (612, None, None, None)
    # approved by the demo superuser for revision 1, dispatched at the requested time, test confirmed
    assert (four.content_revision, four.approved_revision, four.tested_revision) == (1, 1, 1)
    assert (four.approved_at, four.approved_by, four.dispatch_after) == (
        seeds.APPROVED_AT, admin, four.scheduled_at,
    )  # fmt: skip
    assert (four.test_confirmed_at, four.test_confirmed_by) == (seeds.TEST_CONFIRMED_AT, admin)
    assert four.updated_at == seeds.APPROVED_AT
    test_device = TestDevice.objects.get()
    assert (test_device.phone, test_device.label, test_device.registered_by) == (
        seeds.MALIKA_PHONE, seeds.TEST_DEVICE_LABEL, admin,
    )  # fmt: skip
    assert test_device.device.customer == malika and test_device.is_active
    test = CampaignTest.objects.get()
    assert (test.campaign, test.revision, test.test_device, test.device) == (
        four, 1, test_device, test_device.device,
    )  # fmt: skip
    assert (test.fcm_ok, test.sent_at, test.confirmed_at, test.confirmed_by, test.sent_by) == (
        True, seeds.TEST_SENT_AT, seeds.TEST_CONFIRMED_AT, admin, admin,
    )  # fmt: skip
    five = rows[5]
    assert (five.state, five.kind, five.title, five.audience_type) == (
        "draft", "general", "Qish parvarishi bo‘yicha maslahatlar", "all",
    )  # fmt: skip
    assert (five.created_at, five.scheduled_at, five.recipient_count, five.approved_at) == (
        dt(10, 24, 12, 0), None, 4751, None,
    )  # fmt: skip
    assert five.updated_at == five.created_at and five.author == admin
    for row in rows.values():
        assert 3 <= len(row.title) <= 60 and 5 <= len(row.body) <= 180
    # every bookkeeping timestamp is pinned (design 03 §7.1): nothing carries the real clock
    assert not Campaign.objects.filter(updated_at__gt=ANCHOR).exists()
    assert not Campaign.objects.filter(updated_at__lt=models.F("created_at")).exists()
    assert not CustomerSegment.objects.filter(updated_at__gt=ANCHOR).exists()


def test_seed_runs_standalone_without_lower_layers() -> None:
    """No admin, customers or products: segments and campaigns still load with NULL actors/links; without
    Malika's device there is no test device and campaign 4 is approved without a recorded test."""
    seed_notifications(CONTEXT)
    assert counts() == (2, 0, 5, 0, 0, 4, 0, 0)
    assert set(CustomerSegment.objects.values_list("created_by", flat=True)) == {None}
    assert set(Campaign.objects.values_list("author", flat=True)) == {None}
    four = Campaign.objects.get(pk=4)
    assert four.state == "scheduled" and four.approved_by is None and four.tested_revision is None
    three = Campaign.objects.get(pk=3)
    assert three.link_kind == "product" and three.link_product is None
    assert three.link_product_label == "Seramidli namlantiruvchi krem"  # the snapshot keeps the link valid
    assert not Product.objects.exists()
    seed_notifications(CONTEXT)
    assert counts() == (2, 0, 5, 0, 0, 4, 0, 0)


def test_later_seeds_do_not_rewrite_existing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing row keeps its values: a later run with more lower-layer data adds only what is missing."""
    seed_notifications(CONTEXT)
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    accounts_seeds.seed_accounts(CONTEXT)
    seed_notifications(CONTEXT)
    # the test device now exists; campaign 4 was approved without a confirmation and stays so (no rewrite)
    assert counts() == (2, 2, 5, 0, 0, 4, 1, 0)
    assert Campaign.objects.get(pk=1).author is None  # inserted earlier without an admin
    assert SegmentMember.objects.filter(customer_id=5).count() == 2
    assert all(m.added_by is not None for m in SegmentMember.objects.all())
    assert Campaign.objects.filter(kind=enums.NotificationKind.PROMO).count() == 2
    scheduled_at = Campaign.objects.get(pk=4).scheduled_at
    assert scheduled_at is not None and scheduled_at - ANCHOR == timedelta(days=1, hours=21, minutes=55)
