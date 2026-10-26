"""Contract §2.8 segments: CRUD, rule evaluation, members, 409s, exports, the selectors other slices reuse."""

from datetime import datetime
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty.tests.factories import PurchaseFactory
from glowderma.notifications import selectors, services
from glowderma.notifications.models import CustomerSegment, SegmentMember
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CustomerSegmentFactory,
    SegmentMemberFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/segments/"


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def customer_with_purchases(
    online: int, *, store: int = 0, reversed_online: int = 0, store_amount: int = 231_000, **kwargs: Any
) -> Customer:
    """A customer with paid online purchases (one paid order each), in-store purchases and reversed ones."""
    customer = CustomerFactory.create(**kwargs)
    for _ in range(online):
        PurchaseFactory.create(customer=customer)
    for _ in range(store):
        PurchaseFactory.create(customer=customer, store=True, amount=store_amount)
    for _ in range(reversed_online):
        PurchaseFactory.create(customer=customer, reversed=True, reversed_at=ANCHOR)
    return customer


def lifetime(customer: Customer) -> int:
    return sum(p.amount for p in customer.purchases.filter(reversed=False))


# --- rule evaluation ----------------------------------------------------------------------------------------


def test_membership_vectors() -> None:
    three_paid = customer_with_purchases(3, store=1, reversed_online=1)  # store and reversed rows never count
    two_paid = customer_with_purchases(2)
    deleted_three = customer_with_purchases(3, status="deleted", deleted_at=ANCHOR)
    manual_only = CustomerFactory.create()
    orders_rule = CustomerSegmentFactory.create(min_online_orders=3)
    SegmentMemberFactory.create(segment=orders_rule, customer=manual_only)
    SegmentMemberFactory.create(segment=orders_rule, customer=deleted_three)  # never counts
    assert set(selectors.segment_member_ids(orders_rule)) == {three_paid.pk, manual_only.pk}
    assert selectors.matches_rules(orders_rule, three_paid.pk)
    assert not selectors.matches_rules(orders_rule, two_paid.pk)
    assert not selectors.matches_rules(orders_rule, manual_only.pk)
    # lifetime rule: Σ non-reversed purchases of both channels (reversed rows never count)
    big_spender = customer_with_purchases(0, store=2, store_amount=6_000_000)  # 12 mln
    nearly = customer_with_purchases(0, store=1, store_amount=9_999_999, reversed_online=1)
    amount_rule = CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=10_000_000)
    SegmentMemberFactory.create(segment=amount_rule, customer=two_paid)
    assert set(selectors.segment_member_ids(amount_rule)) == {two_paid.pk, big_spender.pk}
    assert selectors.lifetime_totals([three_paid.pk, nearly.pk, big_spender.pk, manual_only.pk]) == {
        three_paid.pk: lifetime(three_paid),
        nearly.pk: 9_999_999,
        big_spender.pk: 12_000_000,
        manual_only.pk: 0,
    }
    # both rules are ANDed: 3 online orders with a lifetime below the amount do not qualify, above they do
    both = CustomerSegmentFactory.create(min_online_orders=3, min_lifetime_amount=lifetime(three_paid) + 1)
    assert set(selectors.segment_member_ids(both)) == set()
    both.min_lifetime_amount = lifetime(three_paid)
    both.save()
    assert set(selectors.segment_member_ids(both)) == {three_paid.pk}
    # no rules: manual only
    manual = CustomerSegmentFactory.create(min_online_orders=None)
    SegmentMemberFactory.create(segment=manual, customer=three_paid)
    assert set(selectors.segment_member_ids(manual)) == {three_paid.pk}
    assert selectors.segments_for_customer(three_paid.pk) == [orders_rule, both, manual]
    assert selectors.segments_for_customer(two_paid.pk) == [amount_rule]
    assert selectors.segments_for_customer(big_spender.pk) == [amount_rule]
    assert selectors.segments_for_customer(deleted_three.pk) == []


def test_segment_stats_count_members_and_pushable_devices() -> None:
    segment = CustomerSegmentFactory.create(min_online_orders=2)
    by_rule = customer_with_purchases(2)
    DeviceFactory.create(customer=by_rule)
    DeviceFactory.create(customer=by_rule, push_enabled=False)  # not pushable: one device counts
    manual = CustomerFactory.create()
    DeviceFactory.create(customer=manual)
    SegmentMemberFactory.create(segment=segment, customer=manual)
    SegmentMemberFactory.create(segment=segment, customer=by_rule)  # manual and by rule: one member
    SegmentMemberFactory.create(segment=segment, customer=CustomerFactory.create())  # no device
    assert selectors.segment_stats(segment) == {"manual_member_count": 3, "member_count": 3, "size": 2}
    assert selectors.segment_size(segment) == 2


# --- CRUD ---------------------------------------------------------------------------------------------------


def test_crud_flow(staff_client: APIClient, staff: StaffUser, clock: Any) -> None:
    created = staff_client.post(
        URL, {"name": " Doimiy mijozlar ", "rules": {"min_online_orders": 3}}, format="json"
    )
    assert created.status_code == 201, created.data
    assert created.data == {
        "id": created.data["id"],
        "name": "Doimiy mijozlar",
        "rules": {"min_online_orders": 3, "min_lifetime_amount": None},
        "manual_member_count": 0,
        "member_count": 0,
        "size": 0,
        "created_at": "2026-10-24T12:05:00+05:00",
        "updated_at": "2026-10-24T12:05:00+05:00",
    }
    pk = created.data["id"]
    assert CustomerSegment.objects.get(pk=pk).created_by == staff
    patched = staff_client.patch(
        f"{URL}{pk}/",
        {"rules": {"min_online_orders": None, "min_lifetime_amount": 10_000_000}},
        format="json",
    )
    assert patched.status_code == 200 and patched.data["rules"] == {
        "min_online_orders": None,
        "min_lifetime_amount": 10_000_000,
    }
    renamed = staff_client.patch(f"{URL}{pk}/", {"name": "10 mln+ xarid"}, format="json")
    assert renamed.status_code == 200 and renamed.data["name"] == "10 mln+ xarid"
    assert staff_client.patch(f"{URL}{pk}/", {"name": "10 mln+ xarid"}, format="json").status_code == 200
    assert staff_client.get(f"{URL}{pk}/").data["name"] == "10 mln+ xarid"
    listed = staff_client.get(URL)
    assert listed.data["count"] == 1 and listed.data["results"][0]["id"] == pk
    assert staff_client.delete(f"{URL}{pk}/").status_code == 204
    assert staff_client.get(f"{URL}{pk}/").status_code == 404
    assert staff_client.delete(f"{URL}{pk}/").status_code == 404
    actions = list(
        AuditEntry.objects.filter(entity_type="segment", entity_id=str(pk))
        .order_by("id")
        .values_list("action", flat=True)
    )
    assert actions == ["segment.created", "segment.updated", "segment.updated", "segment.deleted"]
    updated = AuditEntry.objects.filter(action="segment.updated").order_by("id").first()
    assert updated is not None and updated.before == {"min_online_orders": 3, "min_lifetime_amount": None}
    assert updated.after == {"min_online_orders": None, "min_lifetime_amount": 10_000_000}


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, [("name", "required", "Majburiy maydon.")]),
        ({"name": "A"}, [("name", "min_length", "Kamida 2 ta belgi kiriting.")]),
        ({"name": " A "}, [("name", "min_length", "Kamida 2 ta belgi kiriting.")]),
        ({"name": "x" * 61}, [("name", "max_length", "60 belgidan oshmasin.")]),
        ({"name": "doimiy MIJOZLAR"}, [("name", "unique", "Bu qiymat band.")]),
        ({"name": "Yangi", "rules": {"min_online_orders": 0}},
         [("rules.min_online_orders", "min_value", "Qiymat 1 dan kam bo‘lmasin.")]),
        ({"name": "Yangi", "rules": {"min_lifetime_amount": "abc"}},
         [("rules.min_lifetime_amount", "invalid", "Noto‘g‘ri qiymat.")]),
    ],
)  # fmt: skip
def test_create_validation_table(staff_client: APIClient, body: dict[str, Any], expected: list[Any]) -> None:
    CustomerSegmentFactory.create(name="Doimiy mijozlar")
    response = staff_client.post(URL, body, format="json")
    assert response.status_code == 400 and errors(response) == expected
    assert CustomerSegment.objects.count() == 1


def test_patch_name_uniqueness_ignores_itself(staff_client: APIClient) -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar")
    other = CustomerSegmentFactory.create(name="10 mln+ xarid")
    assert (
        staff_client.patch(f"{URL}{segment.pk}/", {"name": "DOIMIY mijozlar"}, format="json").status_code
        == 200
    )
    taken = staff_client.patch(f"{URL}{segment.pk}/", {"name": other.name.upper()}, format="json")
    assert errors(taken) == [("name", "unique", "Bu qiymat band.")]


def test_delete_409_while_a_scheduled_campaign_targets_the_segment(
    staff_client: APIClient, clock: Any
) -> None:
    segment = CustomerSegmentFactory.create()
    campaign = CampaignFactory.create(
        state="scheduled",
        scheduled_at=datetime(2026, 10, 26, 10, tzinfo=TASHKENT),
        audience_type="segment",
        audience_segment=segment,
        audience_segment_name=segment.name,
    )
    response = staff_client.delete(f"{URL}{segment.pk}/")
    assert response.status_code == 409
    assert (
        response.data["code"] == "conflict"
        and response.data["message"] == "Rejalashtirilgan xabar bu guruhga yuboriladi."
    )
    campaign.state = "queued"
    campaign.scheduled_at = None
    campaign.save()
    assert staff_client.delete(f"{URL}{segment.pk}/").status_code == 204
    campaign.refresh_from_db()
    assert (
        campaign.audience_segment is None and campaign.audience_segment_name == segment.name
    )  # snapshot survives


def test_export_segments(staff_client: APIClient, clock: Any) -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar", min_online_orders=None)
    member = CustomerFactory.create()
    DeviceFactory.create(customer=member)
    SegmentMemberFactory.create(segment=segment, customer=member)
    SegmentMemberFactory.create(segment=segment, customer=CustomerFactory.create())
    response = staff_client.get(URL, {"export": "csv"})
    assert response.status_code == 200
    assert response["Content-Disposition"] == 'attachment; filename="guruhlar-20261024-1205.csv"'
    lines = response.content.decode("utf-8-sig").split("\r\n")
    assert lines[0] == '"ID";"Nomi";"A’zolar";"Push qurilmalar"'
    assert lines[1] == f'"{segment.pk}";"Doimiy mijozlar";"2";"1"'


# --- members ------------------------------------------------------------------------------------------------


def test_members_list_add_remove(staff_client: APIClient, staff: StaffUser, clock: Any) -> None:
    segment = CustomerSegmentFactory.create(min_online_orders=2)
    by_rule = customer_with_purchases(
        2,
        name="Malika Karimova",
        phone="+998901234567",
        registered_at=datetime(2025, 3, 12, 10, 20, tzinfo=TASHKENT),
    )
    DeviceFactory.create(customer=by_rule)
    manual_a = CustomerFactory.create(registered_at=datetime(2025, 5, 1, tzinfo=TASHKENT))
    manual_b = CustomerFactory.create(registered_at=datetime(2025, 6, 1, tzinfo=TASHKENT))
    added = staff_client.post(
        f"{URL}{segment.pk}/members/",
        {"customer_ids": [manual_a.pk, manual_b.pk, manual_b.pk]},
        format="json",
    )
    assert added.status_code == 200, added.data
    assert (added.data["manual_member_count"], added.data["member_count"], added.data["size"]) == (2, 3, 1)
    again = staff_client.post(f"{URL}{segment.pk}/members/", {"customer_ids": [manual_a.pk]}, format="json")
    assert again.status_code == 200 and again.data["manual_member_count"] == 2  # a known member is a no-op
    entries = list(AuditEntry.objects.filter(action="segment.members_added").order_by("id"))
    assert entries[0].after == {
        "customer_ids": [manual_a.pk, manual_b.pk],
        "added": [manual_a.pk, manual_b.pk],
    }
    assert entries[1].after == {"customer_ids": [manual_a.pk], "added": []}

    listed = staff_client.get(f"{URL}{segment.pk}/members/")
    assert listed.status_code == 200 and listed.data["count"] == 3
    assert [m["id"] for m in listed.data["results"]] == [
        manual_b.pk,
        manual_a.pk,
        by_rule.pk,
    ]  # -registered_at
    assert listed.data["results"][2] == {
        "id": by_rule.pk,
        "name": "Malika Karimova",
        "phone": "+998901234567",
        "status": "active",
        "registered_at": "2025-03-12T10:20:00+05:00",
        "lifetime_total": lifetime(by_rule),
        "percent": None,
        "balance": 0,
        "online_orders_count": 2,
    }
    export = staff_client.get(f"{URL}{segment.pk}/members/", {"export": "csv"})
    assert export["Content-Disposition"] == 'attachment; filename="guruh-azolari-20261024-1205.csv"'
    lines = export.content.decode("utf-8-sig").split("\r\n")
    assert (
        lines[0] == '"ID";"Ism";"Telefon";"Ro‘yxatdan o‘tgan";"Jami xaridlar (so‘m)";"Cashback foizi";'
        '"Balans (so‘m)";"Holat"'
    )
    assert (
        # contract 1.8: phones are plain E.164 text (exempt from the formula prefix)
        lines[3] == f'"{by_rule.pk}";"Malika Karimova";"+998901234567";"2025-03-12 10:20";'
        f'"{lifetime(by_rule)}";"";"0";"Faol"'
    )

    # rule-only member: 409; manual: 204; unknown: 404
    conflict = staff_client.delete(f"{URL}{segment.pk}/members/{by_rule.pk}/")
    assert conflict.status_code == 409 and conflict.data["message"] == "Mijoz guruhga qoida orqali kiradi."
    assert staff_client.delete(f"{URL}{segment.pk}/members/{manual_a.pk}/").status_code == 204
    assert not SegmentMember.objects.filter(segment=segment, customer=manual_a).exists()
    assert AuditEntry.objects.get(action="segment.member_removed").after == {"customer_id": manual_a.pk}
    assert staff_client.delete(f"{URL}{segment.pk}/members/{manual_a.pk}/").status_code == 404
    assert staff_client.delete(f"{URL}{segment.pk}/members/999999/").status_code == 404
    assert staff_client.delete(f"{URL}999/members/{manual_b.pk}/").status_code == 404
    assert staff_client.get(f"{URL}999/members/").status_code == 404
    # a manual member who also matches the rules keeps the rule membership after removal
    SegmentMemberFactory.create(segment=segment, customer=by_rule)
    assert staff_client.delete(f"{URL}{segment.pk}/members/{by_rule.pk}/").status_code == 204
    assert staff_client.get(f"{URL}{segment.pk}/members/").data["count"] == 2


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, [("customer_ids", "required", "Majburiy maydon.")]),
        ({"customer_ids": []}, [("customer_ids", "required", "Majburiy maydon.")]),
        ({"customer_ids": "abc"}, [("customer_ids", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"customer_ids": {"a": 1}}, [("customer_ids", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"customer_ids": 5}, [("customer_ids", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"customer_ids": None}, [("customer_ids", "null", "Majburiy maydon.")]),
        ({"customer_ids": list(range(1, 502))}, [("customer_ids", "max_items", "Ko‘pi bilan 500 ta.")]),
        ({"customer_ids": ["x"]}, [("customer_ids.0", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"customer_ids": [999999]}, [("customer_ids", "does_not_exist", "Topilmadi.")]),
    ],
)
def test_members_add_validation(staff_client: APIClient, body: dict[str, Any], expected: list[Any]) -> None:
    segment = CustomerSegmentFactory.create()
    response = staff_client.post(f"{URL}{segment.pk}/members/", body, format="json")
    assert response.status_code == 400, response.data
    assert errors(response) == expected
    assert SegmentMember.objects.count() == 0


def test_members_add_by_phones_and_ids_combined(staff_client: APIClient) -> None:
    segment = CustomerSegmentFactory.create()
    by_id = CustomerFactory.create()
    by_phone = CustomerFactory.create(phone="+998901112233")
    both = CustomerFactory.create(phone="+998901112244")
    response = staff_client.post(
        f"{URL}{segment.pk}/members/",
        {
            "customer_ids": [by_id.pk, both.pk],
            "phones": ["+998901112233", "+998901112244", "+998901112233"],  # duplicates collapse
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["manual_member_count"] == 3
    assert set(SegmentMember.objects.filter(segment=segment).values_list("customer_id", flat=True)) == {
        by_id.pk,
        by_phone.pk,
        both.pk,
    }
    expected_ids = sorted([by_id.pk, by_phone.pk, both.pk])
    assert AuditEntry.objects.get(action="segment.members_added").after == {
        "customer_ids": expected_ids,
        "added": expected_ids,
    }
    only_phones = staff_client.post(
        f"{URL}{segment.pk}/members/", {"customer_ids": [], "phones": ["+998901112233"]}, format="json"
    )
    assert only_phones.status_code == 200 and only_phones.data["manual_member_count"] == 3


def test_members_add_by_phone_errors_name_the_element(staff_client: APIClient) -> None:
    segment = CustomerSegmentFactory.create()
    CustomerFactory.create(phone="+998901112233")
    CustomerFactory.create(phone="+998901112255", status="deleted", deleted_at=ANCHOR)
    unknown = staff_client.post(
        f"{URL}{segment.pk}/members/",
        {"phones": ["+998901112233", "+998909999999", "+998901112255", "+998909999999"]},
        format="json",
    )
    assert unknown.status_code == 400
    message = "Bu raqam bilan mijoz topilmadi."
    assert errors(unknown) == [
        ("phones.1", "customer_not_found", message),  # first occurrence of a duplicate only
        ("phones.2", "customer_not_found", message),  # deleted customers are not resolved
    ]
    malformed = staff_client.post(
        f"{URL}{segment.pk}/members/", {"phones": ["+998901112233", "998901112233"]}, format="json"
    )
    assert errors(malformed) == [("phones.1", "invalid", "Telefon raqamini to‘g‘ri kiriting.")]
    too_many = staff_client.post(
        f"{URL}{segment.pk}/members/", {"phones": ["+998901112233"] * 501}, format="json"
    )
    assert errors(too_many) == [("phones", "max_items", "Ko‘pi bilan 500 ta.")]
    empty = staff_client.post(f"{URL}{segment.pk}/members/", {"phones": []}, format="json")
    assert errors(empty) == [("customer_ids", "required", "Majburiy maydon.")]
    not_a_list = staff_client.post(f"{URL}{segment.pk}/members/", {"phones": "+998901112233"}, format="json")
    assert errors(not_a_list) == [("phones", "invalid", "Noto‘g‘ri qiymat.")]
    assert SegmentMember.objects.count() == 0


def test_deleted_customers_may_be_manual_members_but_never_count(staff_client: APIClient) -> None:
    segment = CustomerSegmentFactory.create()
    gone = CustomerFactory.create(status="deleted", deleted_at=ANCHOR)
    response = staff_client.post(f"{URL}{segment.pk}/members/", {"customer_ids": [gone.pk]}, format="json")
    assert response.status_code == 200
    assert (response.data["manual_member_count"], response.data["member_count"]) == (1, 0)
    assert services.remove_member(segment, gone.pk, actor=None) is None


def test_members_list_and_export_carry_the_loyalty_numbers(staff_client: APIClient) -> None:
    from glowderma.loyalty.tests.factories import LoyaltyAccountFactory

    customer = CustomerFactory.create()
    PurchaseFactory.create(customer=customer, store=True, amount=21_000_000)
    LoyaltyAccountFactory.create(customer=customer, balance=48_600)
    segment = CustomerSegmentFactory.create(
        name="Katta xaridorlar", min_online_orders=None, min_lifetime_amount=20_000_000
    )
    rows = staff_client.get(f"/api/v1/admin/segments/{segment.pk}/members/").json()["results"]
    row = next(item for item in rows if item["id"] == customer.pk)
    assert row["balance"] == 48_600
    assert row["lifetime_total"] == 21_000_000
    export = staff_client.get(f"/api/v1/admin/segments/{segment.pk}/members/?export=csv")
    lines = export.content.decode("utf-8-sig").split("\r\n")
    line = next(line for line in lines if line.startswith(f'"{customer.pk}"'))
    assert '"48600"' in line


def test_segments_for_customer_runs_two_queries_whatever_the_segment_count(
    django_assert_max_num_queries: Any,
) -> None:
    customer = CustomerFactory.create()
    PurchaseFactory.create(customer=customer, store=True, amount=12_000_000)
    expected = []
    for index in range(6):
        segment = CustomerSegmentFactory.create(
            name=f"S{index}", min_online_orders=None, min_lifetime_amount=10_000_000 * (index % 2 + 1)
        )
        if index % 2 == 0:
            expected.append(segment.pk)
    manual = CustomerSegmentFactory.create(name="Qo‘lda", min_online_orders=50)
    SegmentMember.objects.create(segment=manual, customer=customer)
    expected.append(manual.pk)
    with django_assert_max_num_queries(2):
        found = [segment.pk for segment in selectors.segments_for_customer(customer.pk)]
    assert sorted(found) == sorted(expected)
    Customer.objects.filter(pk=customer.pk).update(status="deleted", deleted_at=ANCHOR)
    assert selectors.segments_for_customer(customer.pk) == []
