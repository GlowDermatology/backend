"""Admin handling of accounts without a phone (hybrid identity, admin contract §2.6): list, search, card and
exports show the e-mail; the POS never resolves a phoneless account."""

from __future__ import annotations

import csv
import io

import pytest
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import timeutils
from glowderma.loyalty import qr

pytestmark = pytest.mark.django_db
URL = "/api/v1/admin/customers/"


def test_list_card_search_and_export_show_the_email_of_a_phoneless_account(staff_client: APIClient) -> None:
    phoneless = account_services.create_customer(
        None, name="Dilnoza", language="uz", email="dilnoza@example.uz"
    )
    CustomerFactory.create(name="Bekzod", phone="+998901112233")
    rows = {row["id"]: row for row in staff_client.get(URL).json()["results"]}
    assert (rows[phoneless.pk]["phone"], rows[phoneless.pk]["email"]) == (None, "dilnoza@example.uz")
    found = staff_client.get(URL, {"search": "DILNOZA@EX"}).json()["results"]
    assert [row["id"] for row in found] == [phoneless.pk]
    card = staff_client.get(f"{URL}{phoneless.pk}/").json()
    assert (card["phone"], card["email"]) == (None, "dilnoza@example.uz")
    exported = staff_client.get(URL, {"export": "csv"})
    content = exported.content.decode("utf-8")
    cells = [row for row in csv.reader(io.StringIO(content.lstrip("﻿")), delimiter=";")]
    assert any(row[0] == str(phoneless.pk) and row[2] == "dilnoza@example.uz" for row in cells[1:])


def test_pos_never_resolves_a_phoneless_account(pos_client: APIClient) -> None:
    phoneless = CustomerFactory.create(phone=None, email="x@example.uz", email_verified_at=timeutils.now())
    token = qr.issue_qr(phoneless.pk).token  # me/qr/ refuses such an account; the POS backstops it
    response = pos_client.post("/api/v1/pos/cashback/resolve-qr/", {"token": token}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "qr_invalid"
