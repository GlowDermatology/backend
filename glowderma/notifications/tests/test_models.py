"""notifications models (design 03 §3, §10): every CHECK / unique / partial unique, the data-migration rows,
the indexes in pg_catalog, on_delete behaviour and __str__."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory, StaffUserFactory
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications import media
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
)
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CampaignRecipientFactory,
    CustomerSegmentFactory,
    InboxNotificationFactory,
    SegmentMemberFactory,
    SentCampaignFactory,
    StatusTemplateFactory,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 24, 12, 5, tzinfo=TASHKENT)


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def constraint_names(table: str, kind: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = %s", [table, kind]
        )
        return {row[0] for row in cursor.fetchall()}


# --- StatusTemplate -----------------------------------------------------------------------------------------


def test_four_template_rows_come_from_the_data_migration() -> None:
    rows = list(StatusTemplate.objects.all())
    assert [row.status for row in rows] == ["processing", "handed_to_courier", "delivered", "cancelled"]
    processing = rows[0]
    assert processing.text_uz == "To‘lov qabul qilindi. Buyurtma №{raqam} tayyorlanmoqda."
    assert processing.text_ru == "Оплата принята. Заказ №{raqam} готовится."
    assert processing.text_en == "Payment received. Order №{raqam} is being prepared."
    assert processing.updated_by is None and processing.created_at is not None
    delivered = rows[2]
    assert delivered.text_uz == "Buyurtma №{raqam} yetkazildi. Xaridingiz uchun rahmat."
    assert delivered.text_en == ""  # the mock shows en: false for this row
    assert str(processing) == "Shablon «Jarayonda»: To‘lov qabul qilindi. Buyurtma №{raqam} "
    assert StatusTemplateFactory.create(status="cancelled").pk == rows[3].pk  # get_or_create on status


def test_template_status_unique_and_text_uz_length() -> None:
    violates(
        "notifications_statustemplate_status_key",
        StatusTemplate.objects.create,
        status="processing",
        text_uz="Salom dunyo",
    )
    StatusTemplate.objects.filter(status="processing").delete()
    violates("template_text_uz_5_180", StatusTemplate.objects.create, status="processing", text_uz="Qisq")
    violates("template_text_uz_5_180", StatusTemplate.objects.create, status="processing", text_uz="")
    assert (
        StatusTemplate.objects.create(status="processing", text_uz="x" * 180).text_uz == "x" * 180
    )  # varchar(180)


def test_template_only_raqam_placeholder() -> None:
    StatusTemplate.objects.filter(status="processing").delete()
    create = StatusTemplate.objects.create
    violates(
        "template_only_raqam_placeholder", create, status="processing", text_uz="Salom {ism}, buyurtma tayyor"
    )
    violates(
        "template_only_raqam_placeholder",
        create,
        status="processing",
        text_uz="Buyurtma tayyor",
        text_ru="Заказ {номер}",
    )
    violates(
        "template_only_raqam_placeholder",
        create,
        status="processing",
        text_uz="Buyurtma tayyor",
        text_en="Order {raqam",
    )
    violates(
        "template_only_raqam_placeholder", create, status="processing", text_uz="Buyurtma {raqam } tayyor"
    )
    row = create(status="processing", text_uz="№{raqam} va yana №{raqam} tayyor", text_ru="{raqam}{raqam}")
    assert row.text_uz.count("{raqam}") == 2
    assert constraint_names("notifications_statustemplate", "c") == {
        "template_text_uz_5_180",
        "template_only_raqam_placeholder",
    }


def test_template_updated_by_set_null() -> None:
    staff = StaffUserFactory.create()
    StatusTemplate.objects.filter(status="delivered").update(updated_by=staff)
    staff.delete()
    assert StatusTemplate.objects.get(status="delivered").updated_by is None


# --- CustomerSegment / SegmentMember ------------------------------------------------------------------------


def test_segment_str_defaults_ordering_and_checks() -> None:
    second = CustomerSegmentFactory.create(
        name="10 mln+ xarid", min_online_orders=None, min_lifetime_amount=10_000_000
    )
    first = CustomerSegmentFactory.create(name="Doimiy mijozlar")
    assert (
        str(first) == "Doimiy mijozlar" and first.min_online_orders == 3 and first.min_lifetime_amount is None
    )
    assert list(CustomerSegment.objects.all()) == [second, first]  # id
    assert first.created_by is None
    violates("segment_name_required", CustomerSegmentFactory.create, name="")
    violates("segment_min_orders_positive", CustomerSegmentFactory.create, min_online_orders=0)
    violates("segment_min_amount_positive", CustomerSegmentFactory.create, min_lifetime_amount=0)
    violates("segment_min_amount_positive", CustomerSegmentFactory.create, min_lifetime_amount=-5)
    assert (
        CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=None).min_online_orders
        is None
    )


def test_segment_name_unique_case_insensitively() -> None:
    CustomerSegmentFactory.create(name="VIP")
    violates("segment_name_ci_unique", CustomerSegmentFactory.create, name="vip")
    violates("segment_name_ci_unique", CustomerSegmentFactory.create, name="Vip")
    CustomerSegmentFactory.create(name="VIP 2")
    assert "LOWER" in index_definitions("notifications_customersegment")["segment_name_ci_unique"].upper()


def test_segment_created_by_set_null() -> None:
    staff = StaffUserFactory.create()
    segment = CustomerSegmentFactory.create(created_by=staff)
    staff.delete()
    segment.refresh_from_db()
    assert segment.created_by is None


def test_segment_member_unique_str_ordering_and_cascades() -> None:
    segment = CustomerSegmentFactory.create()
    staff = StaffUserFactory.create()
    later = SegmentMemberFactory.create(segment=segment, added_at=NOW + timedelta(minutes=1))
    earlier = SegmentMemberFactory.create(segment=segment, added_at=NOW, added_by=staff)
    assert str(earlier) == f"Mijoz #{earlier.customer_id} · segment #{segment.pk}"
    assert list(SegmentMember.objects.all()) == [earlier, later]  # segment_id, added_at, id
    assert list(segment.manual_members.all()) == [earlier, later]
    assert list(earlier.customer.segment_memberships.all()) == [earlier]
    violates("segmentmember_unique", SegmentMemberFactory.create, segment=segment, customer=earlier.customer)
    staff.delete()
    earlier.refresh_from_db()
    assert earlier.added_by is None
    earlier.customer.delete()
    assert not SegmentMember.objects.filter(pk=earlier.pk).exists()
    segment.delete()
    assert not SegmentMember.objects.exists()
    assert (
        "USING btree (customer_id)"
        in index_definitions("notifications_segmentmember")["segmentmember_customer_idx"]
    )
    assert "segmentmember_unique" in constraint_names("notifications_segmentmember", "u")


# --- Campaign -----------------------------------------------------------------------------------------------


def test_campaign_defaults_str_and_ordering() -> None:
    older = CampaignFactory.create(title="Birinchi", created_at=NOW - timedelta(days=2))
    scheduled = CampaignFactory.create(
        title="Rejali",
        state="scheduled",
        scheduled_at=NOW + timedelta(days=1),
        created_at=NOW - timedelta(days=3),
    )
    newer = CampaignFactory.create(title="Ikkinchi", created_at=NOW - timedelta(days=1))
    assert list(Campaign.objects.all()) == [
        scheduled,
        newer,
        older,
    ]  # coalesce(scheduled_at, created_at) desc
    assert older.state == "queued" and older.link_kind == "none" and older.recipient_count == 0
    assert older.sent is None and older.failed is None and older.delivered is None
    assert older.send_cursor == 0 and older.batches_dispatched == 0 and older.last_error == ""
    assert older.image.name == "" and older.author is None
    assert str(older) == f"Kampaniya #{older.pk} [Umumiy] Birinchi (Navbatda)"


def test_campaign_text_lengths() -> None:
    violates("campaign_text_lengths", CampaignFactory.create, title="Ab")
    violates("campaign_text_lengths", CampaignFactory.create, title="")
    violates("campaign_text_lengths", CampaignFactory.create, body="Qisq")
    violates("campaign_text_lengths", CampaignFactory.create, body="")
    # the upper bounds are the varchar(60) / varchar(180) column lengths themselves
    assert CampaignFactory.create(title="Abc", body="Salom").title == "Abc"
    assert CampaignFactory.create(title="x" * 60, body="x" * 180).body == "x" * 180


def test_campaign_link_fields() -> None:
    product = ProductFactory.create()
    violates("campaign_link_fields", CampaignFactory.create, link_kind="none", link_product=product)
    violates("campaign_link_fields", CampaignFactory.create, link_kind="none", link_product_label="Krem")
    violates("campaign_link_fields", CampaignFactory.create, link_kind="product", link_product=product)
    with_product = CampaignFactory.create(
        link_kind="product", link_product=product, link_product_label="Krem"
    )
    without = CampaignFactory.create(
        link_kind="product", link_product_label="Krem"
    )  # product gone: label stays
    assert without.link_product is None
    product.delete()
    with_product.refresh_from_db()
    assert with_product.link_product is None and with_product.link_product_label == "Krem"


def test_campaign_audience_fields() -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar")
    customer = CustomerFactory.create(phone="+998901234567")
    create = CampaignFactory.create
    violates("campaign_audience_fields", create, audience_type="all", audience_segment=segment)
    violates("campaign_audience_fields", create, audience_type="all", audience_segment_name="x")
    violates("campaign_audience_fields", create, audience_type="all", audience_customer=customer)
    violates("campaign_audience_fields", create, audience_type="all", audience_phone="+998901234567")
    violates(
        "campaign_audience_fields", create, audience_type="segment", audience_segment=segment
    )  # no snapshot
    violates(
        "campaign_audience_fields",
        create,
        audience_type="segment",
        audience_segment=segment,
        audience_segment_name=segment.name,
        audience_customer=customer,
    )
    violates("campaign_audience_fields", create, audience_type="customer", audience_customer=customer)
    violates(
        "campaign_audience_fields",
        create,
        audience_type="customer",
        audience_customer=customer,
        audience_phone="998901234567",
    )
    violates(
        "campaign_audience_fields",
        create,
        audience_type="customer",
        audience_customer=customer,
        audience_phone=customer.phone,
        audience_segment=segment,
    )
    violates("campaign_audience_fields", create, audience_type="customer", audience_phone=customer.phone)
    by_segment = create(audience_type="segment", audience_segment=segment, audience_segment_name=segment.name)
    by_customer = create(audience_type="customer", audience_customer=customer, audience_phone=customer.phone)
    assert list(segment.campaigns.all()) == [by_segment]
    segment.delete()  # SET_NULL: the sent history keeps its snapshot (N-5)
    by_segment.refresh_from_db()
    assert by_segment.audience_segment is None and by_segment.audience_segment_name == "Doimiy mijozlar"
    with pytest.raises(ProtectedError):
        customer.delete()
    assert by_customer.audience_phone == "+998901234567"


def test_campaign_state_fields() -> None:
    create = CampaignFactory.create
    violates("campaign_state_fields", create, state="scheduled")  # needs scheduled_at
    violates("campaign_state_fields", create, state="scheduled", scheduled_at=NOW, started_at=NOW)
    violates("campaign_state_fields", create, state="queued", started_at=NOW)
    violates("campaign_state_fields", create, state="queued", sent=0)
    violates("campaign_state_fields", create, state="sending")
    violates("campaign_state_fields", create, state="sending", started_at=NOW, sent_at=NOW)
    violates("campaign_state_fields", create, state="sent", started_at=NOW, sent_at=NOW, sent=1)  # failed
    violates("campaign_state_fields", create, state="sent", started_at=NOW, sent_at=NOW, failed=0)
    violates("campaign_state_fields", create, state="sent", sent_at=NOW, sent=1, failed=0)
    assert create(state="scheduled", scheduled_at=NOW).scheduled_at == NOW
    assert create(state="sending", started_at=NOW).started_at == NOW
    sent = SentCampaignFactory.create()
    assert (sent.state, sent.sent, sent.failed, sent.delivered) == ("sent", 10, 0, None)
    violates("campaign_state_fields", create, state="scheduled", scheduled_at=NOW, sent_at=NOW)
    assert constraint_names("notifications_campaign", "c") >= {
        "campaign_text_lengths",
        "campaign_link_fields",
        "campaign_audience_fields",
        "campaign_state_fields",
    }


def test_campaign_author_is_protected() -> None:
    staff = StaffUserFactory.create()
    campaign = CampaignFactory.create(author=staff)
    assert list(staff.campaigns.all()) == [campaign]
    with pytest.raises(ProtectedError):
        staff.delete()


def test_campaign_indexes_in_pg_catalog() -> None:
    indexes = index_definitions("notifications_campaign")
    assert indexes["campaign_at_idx"].endswith("(COALESCE(scheduled_at, created_at) DESC, id DESC)")
    assert indexes["campaign_due_idx"].endswith("(scheduled_at) WHERE ((state)::text = 'scheduled'::text)")
    assert indexes["campaign_state_kind_idx"].endswith("USING btree (state, kind)")
    assert indexes["campaign_sched_segment_idx"].endswith(
        "(audience_segment_id) WHERE ((state)::text = 'scheduled'::text)"
    )


# --- CampaignRecipient --------------------------------------------------------------------------------------


def test_recipient_defaults_str_ordering_and_uniques() -> None:
    recipient = CampaignRecipientFactory.create()
    assert recipient.state == "pending" and recipient.error == "" and recipient.provider_state == {}
    assert recipient.provider_message_id == "" and recipient.sent_at is None and recipient.inbox is None
    assert (
        str(recipient) == f"Kampaniya #{recipient.campaign_id} → mijoz #{recipient.customer_id} (Kutilmoqda)"
    )
    second = CampaignRecipientFactory.create(campaign=recipient.campaign)
    assert list(recipient.campaign.recipients.all()) == [recipient, second]
    violates(
        "recipient_campaign_customer_unique",
        CampaignRecipientFactory.create,
        campaign=recipient.campaign,
        customer=recipient.customer,
    )
    other_customer = CustomerFactory.create()
    violates(
        "recipient_campaign_device_unique",
        CampaignRecipientFactory.create,
        campaign=recipient.campaign,
        customer=other_customer,
        device=recipient.device,
    )
    # the device unique is partial: several recipients without a device are fine
    CampaignRecipientFactory.create(campaign=recipient.campaign, customer=other_customer, device=None)
    CampaignRecipientFactory.create(campaign=recipient.campaign, device=None)
    assert recipient.campaign.recipients.filter(device__isnull=True).count() == 2


def test_recipient_sent_at_matches_state() -> None:
    create = CampaignRecipientFactory.create
    violates("recipient_sent_at", create, state="sent")
    violates("recipient_sent_at", create, state="pending", sent_at=NOW)
    violates("recipient_sent_at", create, state="failed", sent_at=NOW)
    assert create(state="sent", sent_at=NOW).sent_at == NOW
    assert create(state="skipped").sent_at is None


def test_recipient_on_delete_behaviour() -> None:
    inbox = InboxNotificationFactory.create()
    recipient = CampaignRecipientFactory.create(customer=inbox.customer, inbox=inbox)
    assert inbox.campaign_recipient == recipient
    recipient.device.delete()  # type: ignore[union-attr]
    recipient.refresh_from_db()
    assert recipient.device is None
    inbox.delete()
    recipient.refresh_from_db()
    assert recipient.inbox is None
    recipient.campaign.delete()
    assert not CampaignRecipient.objects.exists()
    recipient = CampaignRecipientFactory.create()
    recipient.customer.delete()
    assert not CampaignRecipient.objects.exists()


def test_recipient_indexes_in_pg_catalog() -> None:
    indexes = index_definitions("notifications_campaignrecipient")
    assert indexes["recipient_campaign_state_idx"].endswith("USING btree (campaign_id, state)")
    assert indexes["recipient_customer_idx"].endswith("USING btree (customer_id, created_at DESC)")
    assert indexes["recipient_campaign_device_unique"].startswith("CREATE UNIQUE INDEX")
    assert "WHERE (device_id IS NOT NULL)" in indexes["recipient_campaign_device_unique"]
    assert "recipient_campaign_customer_unique" in constraint_names("notifications_campaignrecipient", "u")


# --- InboxNotification --------------------------------------------------------------------------------------


def test_inbox_defaults_str_and_ordering() -> None:
    older = InboxNotificationFactory.create(created_at=NOW - timedelta(minutes=1))
    newer = InboxNotificationFactory.create(created_at=NOW)
    same = InboxNotificationFactory.create(created_at=NOW)
    assert list(InboxNotification.objects.all()) == [same, newer, older]  # -created_at, -id
    assert older.push_state == "no_device" and older.pushed_at is None and older.push_error == ""
    assert older.read_at is None and older.delivered_at is None and older.image_url == ""
    assert older.template_status == "" and older.link == {"kind": "none"}
    assert str(older) == f"Inbox «Umumiy» → mijoz #{older.customer_id}: Yangi mahsulotlar keldi"
    assert list(older.customer.inbox.all()) == [older]


def test_inbox_dedupe_key_unique_and_text_required() -> None:
    InboxNotificationFactory.create(
        dedupe_key="order_status:10248:processing",
        kind="order_status",
        source_type="order",
        source_id=10248,
        template_status="processing",
    )
    violates(
        "notifications_inboxnotification_dedupe_key_key",
        InboxNotificationFactory.create,
        dedupe_key="order_status:10248:processing",
    )
    violates("inbox_text_required", InboxNotificationFactory.create, title="")
    violates("inbox_text_required", InboxNotificationFactory.create, body="")


def test_inbox_kind_source_fields() -> None:
    create = InboxNotificationFactory.create
    violates("inbox_kind_source_fields", create, kind="order_status", source_type="order")  # template needed
    violates(
        "inbox_kind_source_fields",
        create,
        kind="order_status",
        source_type="campaign",
        template_status="delivered",
    )
    violates(
        "inbox_kind_source_fields",
        create,
        kind="appointment",
        source_type="appointment",
        template_status="delivered",
    )
    violates("inbox_kind_source_fields", create, kind="appointment", source_type="order")
    violates("inbox_kind_source_fields", create, kind="promo", source_type="order")
    violates(
        "inbox_kind_source_fields",
        create,
        kind="general",
        source_type="campaign",
        template_status="delivered",
    )
    assert create(kind="order_status", source_type="order", template_status="delivered", source_id=10248).kind
    assert create(kind="appointment", source_type="appointment", source_id=1).kind == "appointment"
    assert create(kind="promo", source_type="campaign", language=enums.ContentLanguage.RU).language == "ru"
    assert constraint_names("notifications_inboxnotification", "c") == {
        "inbox_kind_source_fields",
        "inbox_text_required",
    }


def test_inbox_customer_cascade_and_indexes() -> None:
    row = InboxNotificationFactory.create()
    row.customer.delete()
    assert not InboxNotification.objects.exists()
    indexes = index_definitions("notifications_inboxnotification")
    assert indexes["inbox_customer_created_idx"].endswith(
        "USING btree (customer_id, created_at DESC, id DESC)"
    )
    assert indexes["inbox_customer_unread_idx"].endswith("USING btree (customer_id) WHERE (read_at IS NULL)")
    assert indexes["inbox_source_idx"].endswith("USING btree (source_type, source_id)")
    assert "notifications_inboxnotification_dedupe_key_key" in constraint_names(
        "notifications_inboxnotification", "u"
    )


def test_device_factory_rows_feed_recipients() -> None:
    device = DeviceFactory.create()
    recipient = CampaignRecipientFactory.create(customer=device.customer, device=device)
    assert recipient.device == device


def test_campaign_image_path_places_the_file_under_the_campaign_folder() -> None:
    campaign = CampaignFactory.create()
    assert media.campaign_image_path(campaign, "abc123.webp") == f"campaigns/{campaign.pk}/abc123.webp"
    assert media.campaign_image_path(CampaignFactory.build(), "dir/x.jpg") == "campaigns/new/x.jpg"
