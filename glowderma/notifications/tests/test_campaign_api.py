"""Contract §2.8: ``GET notifications/``, ``GET notifications/{id}/``, ``GET notifications/meta/`` and the
multipart ``POST notifications/`` that creates a draft (the workflow itself: test_campaign_workflow)."""

from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.notifications import services
from glowderma.notifications.models import Campaign, CampaignRecipient, InboxNotification
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CampaignTestFactory,
    CustomerSegmentFactory,
    ScheduledCampaignFactory,
    SegmentMemberFactory,
    SendingCampaignFactory,
    SentCampaignFactory,
    TestedCampaignFactory,
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
    scheduled = ScheduledCampaignFactory.create(
        title="Rejalashtirilgan",
        scheduled_at=datetime(2026, 10, 26, 10, tzinfo=TASHKENT),
        dispatch_after=datetime(2026, 10, 26, 10, tzinfo=TASHKENT),
        approved_by=staff,
        test_confirmed_by=staff,
        audience_type="segment",
        audience_segment=segment,
        audience_segment_name=segment.name,
        recipient_count=612,
        author=staff,
        created_at=datetime(2026, 10, 24, 11, 30, tzinfo=TASHKENT),
    )
    CampaignTestFactory.create(campaign=scheduled, confirmed_at=ANCHOR - timedelta(minutes=10))
    sending = SendingCampaignFactory.create(
        title="Yuborilmoqda",
        sent=3,
        failed=1,
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
    draft = CampaignFactory.create(title="Qoralama", created_at=datetime(2026, 10, 20, 9, tzinfo=TASHKENT))
    tested = TestedCampaignFactory.create(
        title="Test tasdiqlangan", created_at=datetime(2026, 10, 19, 9, tzinfo=TASHKENT)
    )
    response = staff_client.get(URL)
    assert response.status_code == 200
    body = response.data
    assert (body["count"], body["page"], body["page_size"], body["page_count"]) == (5, 1, 20, 1)
    # coalesce(scheduled_at, created_at) desc: 26 Oct 10:00, 24 Oct 12:00, 20 Oct, 19 Oct, 18 Oct
    assert [r["title"] for r in body["results"]] == [
        "Rejalashtirilgan",
        "Yuborilmoqda",
        "Qoralama",
        "Test tasdiqlangan",
        "Yuborildi",
    ]
    first = body["results"][0]
    actor = {"id": staff.pk, "display_name": staff.full_name}
    assert first == {
        "id": scheduled.pk,
        "kind": "general",
        "title": "Rejalashtirilgan",
        "body": scheduled.body,
        "image_url": None,
        "link": {"kind": "none", "product_id": None, "label": None, "url": None},
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
        "updated_at": first["updated_at"],
        "revision": 1,
        "tested_revision": 1,
        "test_confirmed_at": "2026-10-24T11:55:00+05:00",
        "test_confirmed_by": actor,
        "approved_at": "2026-10-24T12:00:00+05:00",
        "approved_by": actor,
        "approved_revision": 1,
        "dispatch_after": "2026-10-26T10:00:00+05:00",
        "started_at": None,
        "sent_at": None,
        "cancelled_at": None,
        "cancelled_by": None,
        "stop_requested_at": None,
        "stop_requested_by": None,
        "stopped_at": None,
        "failed_at": None,
        "last_error": "",
        "sent": None,
        "failed": None,
        "attempted": None,
        "delivered": None,
        "author": actor,
        "tests": [
            {
                "id": first["tests"][0]["id"],
                "revision": 1,
                "test_device_id": first["tests"][0]["test_device_id"],
                "device_label": first["tests"][0]["device_label"],
                "sent_at": first["tests"][0]["sent_at"],
                "sent_by": None,
                "fcm_ok": True,
                "fcm_error": "",
                "confirmed_at": "2026-10-24T11:55:00+05:00",
                "confirmed_by": None,
            }
        ],
    }
    promo = body["results"][1]
    assert (promo["state"], promo["author"], promo["attempted"]) == ("sending", None, 4)  # live tallies
    assert promo["link"] == {
        "kind": "product",
        "product_id": product.pk,
        "label": "Seramidli namlantiruvchi krem",
        "url": None,
    }
    done = body["results"][4]
    assert (done["state"], done["sent"], done["failed"], done["delivered"]) == ("sent", 10, 0, 7)
    assert done["sent_at"] == "2026-10-24T12:02:00+05:00" and done["tests"] == []

    for state, expected in (
        ("sending", {sending.pk}),
        ("sent", {sent.pk}),
        ("draft", {draft.pk}),
        ("tested", {tested.pk}),
        ("scheduled", {scheduled.pk}),
        ("stopping", set()),
    ):
        assert {r["id"] for r in staff_client.get(URL, {"state": state}).data["results"]} == expected, state
    assert {r["id"] for r in staff_client.get(URL, {"kind": "promo"}).data["results"]} == {
        sending.pk,
        sent.pk,
    }
    assert [
        r["id"] for r in staff_client.get(URL, {"kind": "promo", "state": "scheduled"}).data["results"]
    ] == []
    for bad_state in ("done", "queued"):  # ``queued`` is no longer a state
        bad = staff_client.get(URL, {"state": bad_state})
        assert bad.status_code == 400 and errors(bad) == [
            ("state", "invalid_choice", "Ruxsat etilmagan qiymat.")
        ]
    assert staff_client.get(URL, {"page_size": 7}).status_code == 400
    assert staff_client.get(URL, {"page_size": 50, "page": 9}).data["page"] == 1
    # the detail route: every staff member reads every campaign
    detail = staff_client.get(f"{URL}{scheduled.pk}/")
    assert detail.status_code == 200 and detail.data == first
    assert staff_client.get(f"{URL}999999/").status_code == 404


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
    assert lines[0] == '"ID";"Sana";"Turi";"Sarlavha";"Auditoriya";"Holat";"Yuborildi";"Yetkazildi";"Havola"'
    assert '"2026-10-20 09:00";"Umumiy";"Hammaga";"Barcha · 4 751";"Qoralama";"";"";""' in lines[1]
    assert '"\'=Bitta";"Bitta mijoz · +998 90 123 45 67";"Qoralama"' in lines[2]
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
    assert body["approval_delay_minutes"] == 5  # CAMPAIGN_APPROVAL_DELAY_MINUTES (test settings: the default)


# --- create -------------------------------------------------------------------------------------------------


def test_create_requires_multipart_and_an_idempotency_key(staff_client: APIClient) -> None:
    as_json = staff_client.post(URL, form(), format="json", HTTP_IDEMPOTENCY_KEY=KEY)
    assert as_json.status_code == 415 and as_json.data["code"] == "unsupported_media_type"
    no_key = staff_client.post(URL, form(), format="multipart")
    assert no_key.status_code == 400 and no_key.data["code"] == "idempotency_key_required"


def test_create_makes_a_draft_and_sends_nothing(
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
        "url": None,
    }
    assert record["audience"] == {"type": "all", "segment_id": None, "segment_name": None, "phone": None}
    assert (record["recipient_count"], record["state"], record["revision"]) == (3, "draft", 1)
    assert record["scheduled_at"] is None and record["dispatch_after"] is None and record["tests"] == []
    assert record["created_at"] == "2026-10-24T12:05:00+05:00" and record["author"]["id"] == staff.pk
    assert record["image_url"] is not None and record["image_url"].startswith(
        "http://127.0.0.1:8000/media/campaigns/"
    )
    campaign = Campaign.objects.get(pk=record["id"])
    stored = campaign.image.name or ""
    assert stored.endswith(".png") and stored.startswith(f"campaigns/{campaign.pk}/")
    # a draft: no recipients, no inbox rows, no outbox row, no push, no counters
    assert (campaign.state, campaign.sent, campaign.failed, campaign.started_at) == (
        "draft",
        None,
        None,
        None,
    )
    assert not CampaignRecipient.objects.exists() and not InboxNotification.objects.exists()
    assert OutboxMessage.objects.count() == 0 and fcm.sent == []
    audit = AuditEntry.objects.get(action="notification.created")
    assert audit.entity_id == str(campaign.pk) and audit.after is not None
    assert (audit.after["state"], audit.after["revision"], audit.after["body"]) == ("draft", 1, campaign.body)
    assert audit.after["image"] == stored
    listed = staff_client.get(URL).data["results"][0]
    assert listed["state"] == "draft" and listed["body"] == campaign.body

    # the same form again (a fresh upload object with the same bytes: the first one was consumed)
    data["image"] = SimpleUploadedFile("promo.png", image_bytes(), content_type="image/png")
    replay = post(staff_client, data)
    assert replay.status_code == 201 and replay["Idempotent-Replayed"] == "true"
    assert replay.data == record and Campaign.objects.count() == 1


def test_create_with_a_requested_time_is_still_a_draft(staff_client: APIClient, clock: Any) -> None:
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
    assert response.data["state"] == "draft" and response.data["scheduled_at"] == "2026-10-26T10:00:00+05:00"
    assert (
        response.data["audience"]["segment_name"] == "Doimiy mijozlar"
        and response.data["recipient_count"] == 1
    )
    assert OutboxMessage.objects.count() == 0 and CampaignRecipient.objects.count() == 0
    assert (
        services.start_due_campaigns(datetime(2026, 10, 27, tzinfo=TASHKENT)) == []
    )  # never without approval
    # a draft does not hold its segment; an approved campaign does
    assert staff_client.delete(f"/api/v1/admin/segments/{segment.pk}/").status_code == 204
    held = CustomerSegmentFactory.create(name="Band segment")
    ScheduledCampaignFactory.create(
        audience_type="segment", audience_segment=held, audience_segment_name=held.name
    )
    conflict = staff_client.delete(f"/api/v1/admin/segments/{held.pk}/")
    assert (
        conflict.status_code == 409
        and conflict.data["message"] == "Rejalashtirilgan xabar bu guruhga yuboriladi."
    )


def test_create_for_one_customer_without_devices_is_a_draft_with_no_reach(
    staff_client: APIClient, clock: Any
) -> None:
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
    assert (response.data["state"], response.data["recipient_count"], response.data["sent"]) == (
        "draft",
        0,
        None,
    )
    assert response.data["sent_at"] is None and OutboxMessage.objects.count() == 0


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
        # gap ADM-10.1 (decision GC-3): an https link
        ({"link.kind": "url"}, [("link.url", "required", "Majburiy maydon.")]),
        ({"link.kind": "url", "link.url": ""}, [("link.url", "required", "Majburiy maydon.")]),
        ({"link.kind": "url", "link.url": "http://glowderma.uz/aksiya"},
         [("link.url", "invalid", "Havolani https:// bilan to‘liq kiriting.")]),
        ({"link.kind": "url", "link.url": "glowderma.uz"},
         [("link.url", "invalid", "Havolani https:// bilan to‘liq kiriting.")]),
        ({"link.kind": "url", "link.url": "https://glowderma.uz/" + "a" * 480},
         [("link.url", "max_length", "500 belgidan oshmasin.")]),
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
    assert response.status_code == 201 and response.data["state"] == "draft"
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
    assert response.data["link"] == {
        "kind": "product",
        "product_id": product.pk,
        "label": "B" * 120,
        "url": None,
    }
    assert Campaign.objects.get(pk=response.data["id"]).link_product_label == "B" * 120


def test_meta_sizes_agree_with_the_recipient_count_for_customers_with_two_devices(
    staff_client: APIClient, clock: Any
) -> None:
    """One push per customer (``CampaignRecipient`` is unique on (campaign, customer)): ``total_devices`` and
    the segment ``size`` count reachable customers, so they equal a campaign's ``recipient_count``."""
    segment = CustomerSegmentFactory.create(name="Ikki qurilma", min_online_orders=None)
    two_devices, one_device, no_device = (CustomerFactory.create() for _ in range(3))
    DeviceFactory.create(customer=two_devices, last_active_at=ANCHOR - timedelta(days=2))
    latest = DeviceFactory.create(customer=two_devices, last_active_at=ANCHOR - timedelta(hours=1))
    DeviceFactory.create(customer=one_device)
    DeviceFactory.create(customer=no_device, push_enabled=False, fcm_token="")
    for customer in (two_devices, one_device, no_device):
        SegmentMemberFactory.create(segment=segment, customer=customer)
    meta = staff_client.get(URL + "meta/").data
    assert meta["total_devices"] == 2
    assert [s["size"] for s in meta["segments"] if s["id"] == segment.pk] == [2]
    listed = staff_client.get("/api/v1/admin/segments/").data["results"]
    assert [(s["member_count"], s["size"]) for s in listed if s["id"] == segment.pk] == [(3, 2)]

    audience = {"audience.type": "segment", "audience.segment_id": str(segment.pk)}
    to_segment = post(staff_client, form(**audience))
    requested = post(
        staff_client, form(scheduled_at="2026-10-26T10:00:00+05:00", **audience), key=KEY.replace("6f", "7f")
    )
    everyone = post(staff_client, form(), key=KEY.replace("6f", "8f"))
    assert to_segment.status_code == requested.status_code == everyone.status_code == 201
    assert to_segment.data["recipient_count"] == requested.data["recipient_count"] == 2
    assert everyone.data["recipient_count"] == meta["total_devices"]
    # the send itself (once approved and started) materialises the most recently active device per customer
    campaign = Campaign.objects.get(pk=to_segment.data["id"])
    assert services.materialise(campaign) == 2
    devices = CampaignRecipient.objects.filter(campaign=campaign, customer=two_devices)
    assert list(devices.values_list("device_id", flat=True)) == [latest.pk]


# --- gap ADM-10.1: a campaign that opens an https link (decision GC-3) -------------------------------------


def test_create_with_a_url_link_reaches_the_inbox_and_the_push(
    staff_client: APIClient, staff: StaffUser, clock: Any, fcm: FakeFcm
) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer)
    link = "https://glowderma.uz/aksiya?utm_source=push"
    data = form(**{"link.kind": "url", "link.url": f" {link} "})
    response = post(staff_client, data)
    assert response.status_code == 201, response.data
    assert response.data["link"] == {"kind": "url", "product_id": None, "label": None, "url": link}
    campaign = Campaign.objects.get(pk=response.data["id"])
    assert (campaign.link_kind, campaign.link_url, campaign.link_product_id) == ("url", link, None)
    after = AuditEntry.objects.get(action="notification.created").after or {}
    assert (after["link_kind"], after["link_url"]) == ("url", link)
    # once approved and started, the send carries the link to the inbox and the push
    Campaign.objects.filter(pk=campaign.pk).update(state="sending", started_at=ANCHOR, sent=0, failed=0)
    campaign.refresh_from_db()
    services.materialise(campaign)
    campaign.save()
    services.send_batch(campaign.pk, 0)
    inbox = InboxNotification.objects.get(source_type="campaign", customer=customer)
    assert inbox.link == {"kind": "url", "url": link}
    [message] = fcm.sent
    assert message.data["link_url"] == link and message.data["product_id"] == ""
    # the customer inbox renders it (customer contract §2.12)
    from glowderma.notifications.api.customer.serializers import inbox_link

    assert inbox_link(inbox.link) == {
        "kind": "url",
        "product_id": None,
        "order_id": None,
        "appointment_id": None,
        "url": link,
    }
    assert inbox_link({"kind": "url"})["kind"] == "none"  # a url row without its url is rendered linkless
    # the export names the destination (review nit: column Havola)
    export = staff_client.get(URL, {"export": "csv"}).content.decode("utf-8-sig").split("\r\n")
    assert export[0].endswith(';"Havola"') and export[1].endswith(f';"{link}"')


def test_a_product_link_ignores_a_stray_url(staff_client: APIClient, clock: Any) -> None:
    product = ProductFactory.create(name_uz="Krem")
    data = form(**{"link.kind": "product", "link.product_id": str(product.pk), "link.url": "https://x.uz"})
    response = post(staff_client, data)
    assert response.status_code == 201, response.data
    assert response.data["link"]["url"] is None
    assert Campaign.objects.get(pk=response.data["id"]).link_url == ""
    # review nit: an ignored stray URL is not validated either
    data = form(**{"link.kind": "product", "link.product_id": str(product.pk), "link.url": "http://x"})
    assert post(staff_client, data, key="8d0f5a43-3c8e-4d43-9a8a-3b7c6f1e2a90").status_code == 201


@pytest.mark.parametrize("scheme", ["HTTPS", "Https"])
def test_an_upper_case_scheme_is_stored_lower_case_not_a_500(
    staff_client: APIClient, clock: Any, scheme: str
) -> None:
    """Review fix: URLValidator accepts ``HTTPS://``; the DB check is case-sensitive → normalise, no 500."""
    response = post(staff_client, form(**{"link.kind": "url", "link.url": f"{scheme}://glowderma.uz/Aksiya"}))
    assert response.status_code == 201, response.data
    assert response.data["link"]["url"] == "https://glowderma.uz/Aksiya"
    assert Campaign.objects.get(pk=response.data["id"]).link_url == "https://glowderma.uz/Aksiya"


def test_database_refuses_inconsistent_url_links() -> None:
    from django.db import IntegrityError, transaction

    from glowderma.notifications.tests.factories import CampaignFactory

    product = ProductFactory.create()
    for fields in (
        {"link_kind": "url", "link_url": "http://x.uz"},
        {"link_kind": "url", "link_url": ""},
        {"link_kind": "url", "link_url": "https://x.uz", "link_product": product, "link_product_label": "K"},
        {"link_kind": "none", "link_url": "https://x.uz"},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            CampaignFactory.create(**fields)
