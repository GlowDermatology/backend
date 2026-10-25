"""Contract §2.8: ``GET notifications/``, ``GET notifications/meta/``, multipart ``POST notifications/``."""

from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.notifications.models import Campaign, CampaignRecipient, InboxNotification
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CustomerSegmentFactory,
    SegmentMemberFactory,
    SentCampaignFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/"
KEY = "6f1c2a0e-9b8d-4c37-8a1e-0123456789ab"


def image_bytes(fmt: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (40, 30), "#d6b2c4").save(output, format=fmt)
    return output.getvalue()


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def form(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "general",
        "title": "Yangi mahsulotlar keldi",
        "body": "Serumlar va tonerlar bo‘limida yangi mahsulotlar. Ilovada ko‘ring.",
        "audience.type": "all",
    }
    data.update(overrides)
    return data


def post(client: APIClient, data: dict[str, Any], key: str = KEY) -> Any:
    return client.post(URL, data, format="multipart", HTTP_IDEMPOTENCY_KEY=key)


# --- list and meta ------------------------------------------------------------------------------------------


def test_list_shape_ordering_and_filters(staff_client: APIClient, staff: StaffUser, clock: Any) -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar")
    product = ProductFactory.create(name_uz="Seramidli namlantiruvchi krem")
    scheduled = CampaignFactory.create(
        title="Rejalashtirilgan",
        state=Campaign.State.SCHEDULED,
        scheduled_at=datetime(2026, 10, 26, 10, tzinfo=TASHKENT),
        audience_type="segment",
        audience_segment=segment,
        audience_segment_name=segment.name,
        recipient_count=612,
        author=staff,
        created_at=datetime(2026, 10, 24, 11, 30, tzinfo=TASHKENT),
    )
    sending = CampaignFactory.create(
        title="Yuborilmoqda",
        state=Campaign.State.SENDING,
        started_at=ANCHOR,
        kind="promo",
        link_kind="product",
        link_product=product,
        link_product_label=product.name_uz,
        created_at=datetime(2026, 10, 24, 12, tzinfo=TASHKENT),
    )
    sent = SentCampaignFactory.create(
        title="Yuborildi",
        created_at=datetime(2026, 10, 18, 16, 30, tzinfo=TASHKENT),
        delivered=7,
        kind="promo",
    )
    queued = CampaignFactory.create(title="Navbatda", created_at=datetime(2026, 10, 20, 9, tzinfo=TASHKENT))
    response = staff_client.get(URL)
    assert response.status_code == 200
    body = response.data
    assert (body["count"], body["page"], body["page_size"], body["page_count"]) == (4, 1, 20, 1)
    # coalesce(scheduled_at, created_at) desc: 26 Oct 10:00, 24 Oct 12:00, 20 Oct, 18 Oct
    assert [r["title"] for r in body["results"]] == [
        "Rejalashtirilgan",
        "Yuborilmoqda",
        "Navbatda",
        "Yuborildi",
    ]
    first = body["results"][0]
    assert first == {
        "id": scheduled.pk,
        "kind": "general",
        "title": "Rejalashtirilgan",
        "body": scheduled.body,
        "image_url": None,
        "link": {"kind": "none", "product_id": None, "label": None},
        "audience": {
            "type": "segment",
            "segment_id": segment.pk,
            "segment_name": "Doimiy mijozlar",
            "phone": None,
        },
        "recipient_count": 612,
        "state": "scheduled",
        "scheduled_at": "2026-10-26T10:00:00+05:00",
        "created_at": "2026-10-24T11:30:00+05:00",
        "sent_at": None,
        "sent": None,
        "failed": None,
        "delivered": None,
        "author": {"id": staff.pk, "display_name": staff.full_name},
    }
    promo = body["results"][1]
    assert promo["state"] == "queued" and promo["author"] is None  # sending renders as queued
    assert promo["link"] == {
        "kind": "product",
        "product_id": product.pk,
        "label": "Seramidli namlantiruvchi krem",
    }
    done = body["results"][3]
    assert (done["state"], done["sent"], done["failed"], done["delivered"]) == ("sent", 10, 0, 7)
    assert done["sent_at"] == "2026-10-24T12:02:00+05:00"

    queued_ids = {r["id"] for r in staff_client.get(URL, {"state": "queued"}).data["results"]}
    assert queued_ids == {sending.pk, queued.pk}
    assert [r["id"] for r in staff_client.get(URL, {"state": "sent"}).data["results"]] == [sent.pk]
    assert {r["id"] for r in staff_client.get(URL, {"kind": "promo"}).data["results"]} == {
        sending.pk,
        sent.pk,
    }
    assert [
        r["id"] for r in staff_client.get(URL, {"kind": "promo", "state": "scheduled"}).data["results"]
    ] == []
    bad = staff_client.get(URL, {"state": "done"})
    assert bad.status_code == 400 and errors(bad) == [("state", "invalid_choice", "Ruxsat etilmagan qiymat.")]
    assert staff_client.get(URL, {"page_size": 7}).status_code == 400
    assert staff_client.get(URL, {"page_size": 50, "page": 9}).data["page"] == 1


def test_export_csv_and_xlsx(staff_client: APIClient, clock: Any) -> None:
    SentCampaignFactory.create(
        title="Quyoshdan himoya",
        audience_type="segment",
        audience_segment_name="10 mln+ xarid",
        recipient_count=318,
        sent=318,
        delivered=301,
        kind="promo",
        created_at=datetime(2026, 10, 18, 16, 30, tzinfo=TASHKENT),
    )
    customer = CustomerFactory.create(phone="+998901234567")
    CampaignFactory.create(
        title="=Bitta",
        audience_type="customer",
        audience_customer=customer,
        audience_phone=customer.phone,
        recipient_count=1,
        created_at=datetime(2026, 10, 19, 9, tzinfo=TASHKENT),
    )
    CampaignFactory.create(
        title="Hammaga", recipient_count=4751, created_at=datetime(2026, 10, 20, 9, tzinfo=TASHKENT)
    )
    response = staff_client.get(URL, {"export": "csv", "kind": "general"})
    assert response.status_code == 200
    assert response["Content-Disposition"] == 'attachment; filename="bildirishnomalar-20261024-1205.csv"'
    lines = response.content.decode("utf-8-sig").split("\r\n")
    assert lines[0] == '"ID";"Sana";"Turi";"Sarlavha";"Auditoriya";"Holat";"Yuborildi";"Yetkazildi"'
    assert '"2026-10-20 09:00";"Umumiy";"Hammaga";"Barcha · 4 751";"Navbatda";"";""' in lines[1]
    assert '"\'=Bitta";"Bitta mijoz · +998 90 123 45 67";"Navbatda"' in lines[2]
    full = staff_client.get(URL, {"export": "xlsx"})
    assert full.status_code == 200 and full["Content-Type"].startswith("application/vnd.openxmlformats")
    audit = AuditEntry.objects.filter(action="export.downloaded").order_by("-id").first()
    assert audit is not None and audit.after == {
        "resource": "notifications",
        "filters": {},
        "row_count": 3,
        "format": "xlsx",
    }
    assert staff_client.get(URL, {"export": "pdf"}).status_code == 400


def test_meta(staff_client: APIClient, staff: StaffUser) -> None:
    active = CustomerFactory.create()
    DeviceFactory.create(customer=active)
    DeviceFactory.create(customer=active, push_enabled=False)  # not pushable
    DeviceFactory.create(customer=active, is_active=False)  # gone
    deleted = CustomerFactory.create(status="deleted", deleted_at=ANCHOR)
    DeviceFactory.create(customer=deleted)
    loyal = CustomerSegmentFactory.create(name="Doimiy mijozlar", min_online_orders=3)
    SegmentMemberFactory.create(segment=loyal, customer=active)
    SegmentMemberFactory.create(segment=loyal, customer=deleted)
    empty = CustomerSegmentFactory.create(name="Bo‘sh", min_online_orders=None)
    ProductFactory.create(name_uz="Yuz uchun krem")
    ProductFactory.create(name_uz="", billz_name="BILLZ TONER")
    ProductFactory.create(name_uz="Afsona serum", status="hidden")
    ProductFactory.create(name_uz="aloe gel")
    response = staff_client.get(URL + "meta/")
    assert response.status_code == 200
    body = response.data
    assert body["total_devices"] == 1
    assert body["segments"] == [
        {"id": loyal.pk, "name": "Doimiy mijozlar", "size": 1},
        {"id": empty.pk, "name": "Bo‘sh", "size": 0},
    ]
    assert [t["status"] for t in body["templates"]] == [
        "processing",
        "handed_to_courier",
        "delivered",
        "cancelled",
    ]
    assert body["templates"][0]["texts"] == {
        "uz": "To‘lov qabul qilindi. Buyurtma №{raqam} tayyorlanmoqda.",
        "ru": "Оплата принята. Заказ №{raqam} готовится.",
        "en": "Payment received. Order №{raqam} is being prepared.",
    }
    assert body["templates"][2]["texts"]["en"] == "" and body["templates"][0]["updated_by"] is None
    assert [o["label"] for o in body["link_options"]] == ["aloe gel", "BILLZ TONER", "Yuz uchun krem"]
    assert all(set(o) == {"product_id", "label"} for o in body["link_options"])


# --- create -------------------------------------------------------------------------------------------------


def test_create_requires_multipart_and_an_idempotency_key(staff_client: APIClient) -> None:
    as_json = staff_client.post(URL, form(), format="json", HTTP_IDEMPOTENCY_KEY=KEY)
    assert as_json.status_code == 415 and as_json.data["code"] == "unsupported_media_type"
    no_key = staff_client.post(URL, form(), format="multipart")
    assert no_key.status_code == 400 and no_key.data["code"] == "idempotency_key_required"


@override_settings(CAMPAIGN_BATCH_SIZE=500)
def test_create_queued_sends_end_to_end(
    staff_client: APIClient,
    staff: StaffUser,
    clock: Any,
    fcm: FakeFcm,
    django_capture_on_commit_callbacks: Any,
) -> None:
    product = ProductFactory.create(name_uz="Seramidli namlantiruvchi krem")
    reachable = [CustomerFactory.create(language="ru") for _ in range(3)]
    for customer in reachable:
        DeviceFactory.create(customer=customer)
    dead_token = CustomerFactory.create()
    DeviceFactory.create(customer=dead_token, fcm_token="tok-bad")  # noqa: S106
    silent = CustomerFactory.create()
    DeviceFactory.create(customer=silent, push_enabled=False, fcm_token="")
    deleted = CustomerFactory.create(status="deleted", deleted_at=ANCHOR)
    DeviceFactory.create(customer=deleted)
    data = form(
        kind="promo",
        title="  Kuzgi namlik to‘plami  ",
        body="Tanlangan namlantiruvchi kremlarga 20% chegirma — 30-noyabrgacha.",
        image=SimpleUploadedFile("promo.png", image_bytes(), content_type="image/png"),
        **{"link.kind": "product", "link.product_id": str(product.pk)},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = post(staff_client, data)
    assert response.status_code == 201, response.data
    record = response.data
    assert record["title"] == "Kuzgi namlik to‘plami" and record["kind"] == "promo"
    assert record["link"] == {
        "kind": "product",
        "product_id": product.pk,
        "label": "Seramidli namlantiruvchi krem",
    }
    assert record["audience"] == {"type": "all", "segment_id": None, "segment_name": None, "phone": None}
    assert record["recipient_count"] == 4 and record["state"] == "queued" and record["scheduled_at"] is None
    assert record["created_at"] == "2026-10-24T12:05:00+05:00" and record["author"]["id"] == staff.pk
    assert record["image_url"] is not None and record["image_url"].startswith(
        "http://127.0.0.1:8000/media/campaigns/"
    )
    campaign = Campaign.objects.get(pk=record["id"])
    stored = campaign.image.name or ""
    assert stored.endswith(".png") and stored.startswith(f"campaigns/{campaign.pk}/")

    # the outbox dispatched eagerly on commit: recipients, inbox rows, counters, state sent
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed) == ("sent", 3, 1)
    assert campaign.started_at == campaign.sent_at == ANCHOR and campaign.batches_dispatched == 1
    recipients = {r.customer_id: r for r in CampaignRecipient.objects.filter(campaign=campaign)}
    assert set(recipients) == {c.pk for c in reachable} | {dead_token.pk}
    assert all(recipients[c.pk].state == "sent" and recipients[c.pk].provider_message_id for c in reachable)
    assert recipients[dead_token.pk].state == "failed" and recipients[dead_token.pk].error == "unregistered"
    assert campaign.send_cursor == max(r.pk for r in recipients.values())
    inbox = {i.customer_id: i for i in InboxNotification.objects.filter(source_type="campaign")}
    assert set(inbox) == set(recipients)
    row = inbox[reachable[0].pk]
    assert (row.kind, row.title, row.push_state, row.language) == (
        "promo",
        "Kuzgi namlik to‘plami",
        "sent",
        "uz",
    )
    assert row.link == {"kind": "product", "product_id": product.pk} and row.image_url == record["image_url"]
    assert row.dedupe_key == f"campaign:{campaign.pk}:{reachable[0].pk}" and row.pushed_at == ANCHOR
    assert inbox[dead_token.pk].push_state == "failed" and inbox[dead_token.pk].push_error == "unregistered"
    assert recipients[reachable[0].pk].inbox == row
    assert len(fcm.sent) == 3 and fcm.sent[0].data == {"type": "campaign", "campaign_id": str(campaign.pk)}
    assert OutboxMessage.objects.get(topic="campaign.send_batch").status == "done"
    audit = AuditEntry.objects.get(action="notification.created")
    assert (
        audit.entity_id == str(campaign.pk) and audit.after is not None and audit.after["state"] == "queued"
    )
    listed = staff_client.get(URL).data["results"][0]
    assert listed["state"] == "sent" and listed["sent"] == 3 and listed["failed"] == 1

    # the same form again (a fresh upload object with the same bytes: the first one was consumed)
    data["image"] = SimpleUploadedFile("promo.png", image_bytes(), content_type="image/png")
    replay = post(staff_client, data)
    assert replay.status_code == 201 and replay["Idempotent-Replayed"] == "true"
    assert Campaign.objects.count() == 1


def test_create_scheduled_waits_for_beat(staff_client: APIClient, clock: Any) -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar", min_online_orders=None)
    member = CustomerFactory.create()
    DeviceFactory.create(customer=member)
    SegmentMemberFactory.create(segment=segment, customer=member)
    SegmentMemberFactory.create(segment=segment, customer=CustomerFactory.create())  # no device
    data = form(
        **{"audience.type": "segment", "audience.segment_id": str(segment.pk)},
        scheduled_at="2026-10-26T10:00:00+05:00",
    )
    response = post(staff_client, data)
    assert response.status_code == 201, response.data
    assert (
        response.data["state"] == "scheduled" and response.data["scheduled_at"] == "2026-10-26T10:00:00+05:00"
    )
    assert (
        response.data["audience"]["segment_name"] == "Doimiy mijozlar"
        and response.data["recipient_count"] == 1
    )
    assert OutboxMessage.objects.count() == 0 and CampaignRecipient.objects.count() == 0
    # a segment with a scheduled campaign cannot be deleted
    conflict = staff_client.delete(f"/api/v1/admin/segments/{segment.pk}/")
    assert (
        conflict.status_code == 409
        and conflict.data["message"] == "Rejalashtirilgan xabar bu guruhga yuboriladi."
    )


def test_create_for_one_customer_without_devices_is_sent_at_once(staff_client: APIClient, clock: Any) -> None:
    customer = CustomerFactory.create(phone="+998901234567")
    data = form(**{"audience.type": "customer", "audience.phone": customer.phone})
    response = post(staff_client, data)
    assert response.status_code == 201, response.data
    assert response.data["audience"] == {
        "type": "customer",
        "segment_id": None,
        "segment_name": None,
        "phone": customer.phone,
    }
    assert (
        response.data["state"],
        response.data["recipient_count"],
        response.data["sent"],
        response.data["failed"],
    ) == ("sent", 0, 0, 0)
    assert response.data["sent_at"] == "2026-10-24T12:05:00+05:00" and OutboxMessage.objects.count() == 0


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"kind": "news"}, [("kind", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
        ({"title": " ab "}, [("title", "min_length", "Sarlavhani kiriting.")]),
        ({"title": ""}, [("title", "min_length", "Sarlavhani kiriting.")]),
        ({"title": "   "}, [("title", "min_length", "Sarlavhani kiriting.")]),
        ({"body": ""}, [("body", "min_length", "Matnni kiriting.")]),
        ({"body": "    "}, [("body", "min_length", "Matnni kiriting.")]),
        ({"title": "x" * 61}, [("title", "max_length", "60 belgidan oshmasin.")]),
        ({"body": "abcd"}, [("body", "min_length", "Matnni kiriting.")]),
        ({"body": "x" * 181}, [("body", "max_length", "180 belgidan oshmasin.")]),
        ({"link.kind": "category"}, [("link.kind", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
        ({"link.kind": "product"}, [("link.product_id", "required", "Majburiy maydon.")]),
        ({"link.kind": "product", "link.product_id": "999999"},
         [("link.product_id", "does_not_exist", "Mahsulot topilmadi yoki yashirin.")]),
        ({"audience.type": "group"}, [("audience.type", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
        ({"audience.type": "segment"}, [("audience.segment_id", "required", "Majburiy maydon.")]),
        ({"audience.type": "segment", "audience.segment_id": "999"},
         [("audience.segment_id", "does_not_exist", "Topilmadi.")]),
        ({"audience.type": "customer"}, [("audience.phone", "required", "Majburiy maydon.")]),
        ({"audience.type": "customer", "audience.phone": "901234567"},
         [("audience.phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")]),
        ({"audience.type": "customer", "audience.phone": "+998900000009"},
         [("audience.phone", "customer_not_found", "Bu raqam bilan mijoz topilmadi.")]),
        ({"scheduled_at": "2026-10-24T12:05:00+05:00"},
         [("scheduled_at", "in_past", "Kelajakdagi vaqtni tanlang.")]),
        ({"scheduled_at": "2026-10-26T10:00:00"}, [("scheduled_at", "invalid", "Noto‘g‘ri qiymat.")]),
    ],
)  # fmt: skip
def test_create_validation_table(
    staff_client: APIClient, clock: Any, overrides: dict[str, Any], expected: list[Any]
) -> None:
    hidden = ProductFactory.create(status="hidden")
    if overrides.get("link.product_id") == "999999":
        overrides = {**overrides, "link.product_id": str(hidden.pk)}
    response = post(staff_client, form(**overrides))
    assert response.status_code == 400, response.data
    assert (
        response.data["code"] == "validation_error" and response.data["message"] == "Maydonlarni tekshiring."
    )
    assert errors(response) == expected
    assert (
        Campaign.objects.count() == 0
        and not AuditEntry.objects.filter(action="notification.created").exists()
    )


def test_create_missing_fields_and_product_link_with_a_deleted_customer_phone(
    staff_client: APIClient, clock: Any
) -> None:
    response = post(staff_client, {})
    assert errors(response) == [
        ("kind", "required", "Majburiy maydon."),
        ("title", "required", "Majburiy maydon."),
        ("body", "required", "Majburiy maydon."),
        ("audience.type", "required", "Majburiy maydon."),
    ]
    gone = CustomerFactory.create(status="deleted", deleted_at=ANCHOR)
    response = post(staff_client, form(**{"audience.type": "customer", "audience.phone": gone.phone}))
    assert errors(response) == [("audience.phone", "customer_not_found", "Bu raqam bilan mijoz topilmadi.")]


@pytest.mark.parametrize(
    "upload,code,message",
    [
        (SimpleUploadedFile("x.txt", b"not an image", content_type="text/plain"),
         "unsupported_image", "Faqat JPEG, PNG yoki WebP rasm."),
        (SimpleUploadedFile("big.png", b"\x89PNG" + b"0" * (5 * 1024 * 1024), content_type="image/png"),
         "file_too_large", "Rasm 5 MB dan oshmasin."),
    ],
)  # fmt: skip
def test_create_image_rules(
    staff_client: APIClient, clock: Any, upload: Any, code: str, message: str
) -> None:
    response = post(staff_client, form(image=upload))
    assert response.status_code == 400 and errors(response) == [("image", code, message)]
    assert Campaign.objects.count() == 0


def test_create_image_is_reencoded_and_a_different_body_reuses_no_key(
    staff_client: APIClient, clock: Any
) -> None:
    response = post(
        staff_client, form(image=SimpleUploadedFile("a.jpg", image_bytes("JPEG"), content_type="image/jpeg"))
    )
    assert response.status_code == 201 and response.data["image_url"].endswith(".jpg")
    other = post(staff_client, form(title="Boshqa sarlavha"))
    assert other.status_code == 422 and other.data["code"] == "idempotency_key_reused"


def test_scheduled_at_far_future_and_timezones(staff_client: APIClient, clock: Any) -> None:
    response = post(staff_client, form(scheduled_at=(ANCHOR + timedelta(minutes=1)).isoformat()))
    assert response.status_code == 201 and response.data["state"] == "scheduled"
    assert response.data["scheduled_at"] == "2026-10-24T12:06:00+05:00"
    zulu = post(staff_client, form(scheduled_at="2026-10-26T05:00:00Z"), key=KEY.replace("a", "b"))
    assert zulu.status_code == 201 and zulu.data["scheduled_at"] == "2026-10-26T10:00:00+05:00"


def test_create_cuts_a_long_billz_name_label_to_the_snapshot_column(
    staff_client: APIClient, clock: Any
) -> None:
    """A product without an Uzbek name falls back to ``billz_name`` (≤255); the snapshot column holds 120."""
    product = ProductFactory.create(name_uz="", billz_name="B" * 200, status="active")
    response = post(staff_client, form(**{"link.kind": "product", "link.product_id": str(product.pk)}))
    assert response.status_code == 201, response.data
    assert response.data["link"] == {"kind": "product", "product_id": product.pk, "label": "B" * 120}
    assert Campaign.objects.get(pk=response.data["id"]).link_product_label == "B" * 120
