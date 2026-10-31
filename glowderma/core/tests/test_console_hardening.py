"""Security hardening of the ops console (glowderma.core): no password change page, secrets masked in JSON,
the console bound to its own host."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import models
from django.test import Client, RequestFactory

from glowderma.accounts.tests.factories import TEST_PASSWORD
from glowderma.accounts.tests.ops_helpers import ops_login
from glowderma.core.admin import ReadOnlyAdmin, ReadOnlyTabularInline, column_field, render_json
from glowderma.core.jsonmask import MASK, is_secret_key, mask_secrets

pytestmark = pytest.mark.django_db

PASSWORD_CHANGE_URLS = ("/django-admin/password_change/", "/django-admin/password_change/done/")


def verified_client() -> tuple[Client, Any]:
    client = Client()
    user = ops_login(client, get_user_model().objects.create_user("ops@x.uz", TEST_PASSWORD, is_staff=True))
    return client, user


# --- finding 1: the stock password change is not a console page ------------------------------------------


@pytest.mark.parametrize("url", PASSWORD_CHANGE_URLS)
def test_the_stock_password_change_pages_answer_404(url: str) -> None:
    client, user = verified_client()
    assert client.get(url).status_code == 404
    answer = client.post(
        url,
        {"old_password": TEST_PASSWORD, "new_password1": "Brand-New-Passw0rd!x", "new_password2": "x"},
    )
    assert answer.status_code == 404
    user.refresh_from_db()
    assert user.check_password(TEST_PASSWORD)


@pytest.mark.parametrize("url", PASSWORD_CHANGE_URLS)
def test_the_password_change_pages_still_send_strangers_to_the_login(url: str) -> None:
    answer = Client().get(url)
    assert answer.status_code == 302 and answer["Location"].startswith("/django-admin/login/")


def test_the_user_menu_has_no_password_link_but_the_users_own_record() -> None:
    client, user = verified_client()
    content = client.get("/django-admin/").content.decode()
    assert "password_change" not in content
    assert "Change password" not in content and "Parolni o" not in content
    own = f"/django-admin/accounts/staffuser/{user.pk}/change/"
    assert f'href="{own}"' in content and "Mening hisobim" in content
    assert client.get(own).status_code == 200


# --- finding 2: secrets never reach a console page ------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "sealed_token",
        "farewell_tokens",
        "fcm_token",
        "fcmToken",
        "provider_refresh_token",
        "refresh",
        "access_token",
        "id_token",
        "password",
        "new_password",
        "client_secret",
        "SECRET_KEY",
        "key_hash",
        "refresh_hash",
        "code_hmac",
        "Authorization",
        "HTTP_AUTHORIZATION",
        "X-GD-Signature",
        "signature",
        "sign_string",
        "sign",
        "api_key",
        "x-api-key",
        "apiKey",
        "private_key",
        "key",
        "otp",
        "pin_code",
        "verification_code",
        "auth_code",
        "Cookie",
        "credentials",
        "assertion",
    ],
)
def test_secret_like_keys_are_masked(key: str) -> None:
    assert is_secret_key(key)
    assert mask_secrets({key: "VALUE-MARKER"}) == {key: MASK}


@pytest.mark.parametrize(
    "key",
    [
        "order_id",
        "identity_id",
        "customer_id",
        "client_id",
        "sms_code_id",
        "dedupe_key",
        "event_key",
        "error_code",
        "code",
        "status_code",
        "event",
        "event_id",
        "provider",
        "click_paydoc_id",
        "click_trans_id",
        "sign_time",
        "amount",
        "provider_txn_id",
        "refreshed_at",
        "keyword",
        "message",
        "kind",
        "lang",
        "to",
        "Content-Type",
        "User-Agent",
        "X-Forwarded-For",
    ],
)
def test_ordinary_keys_stay_readable(key: str) -> None:
    assert not is_secret_key(key)
    assert mask_secrets({key: "VALUE"}) == {key: "VALUE"}


def test_masking_is_recursive_and_keeps_empty_values() -> None:
    document = {
        "order_id": 7,
        "nested": {"tokens": ["a", "b"], "inner": [{"fcm_token": "x", "sid": "s"}], "ok": True},
        "farewell_tokens": [],
        "sealed_token": None,
        "secret_flag": False,
        "headers": {"Authorization": {"scheme": "Basic", "value": "abc"}},
    }
    assert mask_secrets(document) == {
        "order_id": 7,
        "nested": {"tokens": MASK, "inner": [{"fcm_token": MASK, "sid": "s"}], "ok": True},
        "farewell_tokens": [],
        "sealed_token": None,
        "secret_flag": False,
        "headers": {"Authorization": MASK},
    }
    assert document["nested"]["tokens"] == ["a", "b"]  # type: ignore[index]  # a copy, never in place


def test_render_json_masks_and_escapes() -> None:
    html = str(render_json({"sealed_token": "SEALED", "note": "<b>x</b>"}))
    assert "SEALED" not in html and MASK in html and "&lt;b&gt;" in html
    assert render_json(None) == "-"


def _admin_request() -> Any:
    request = RequestFactory().get("/django-admin/")
    request.user = get_user_model().objects.create_user("look@x.uz", TEST_PASSWORD, is_staff=True)
    return request


def test_every_json_field_of_the_console_goes_through_the_masking_renderer() -> None:
    request = _admin_request()
    seen = 0
    for model, model_admin in admin.site._registry.items():
        admins: list[Any] = [model_admin, *model_admin.get_inline_instances(request, None)]
        for each in admins:
            json_fields = [
                f.name for f in each.model._meta.concrete_fields if isinstance(f, models.JSONField)
            ]
            if not json_fields:
                continue
            assert isinstance(each, ReadOnlyAdmin | ReadOnlyTabularInline), (model, type(each))
            shown = each.get_readonly_fields(request, None)
            listed = [column_field(entry) for entry in getattr(each, "list_display", ())]
            for name in json_fields:
                assert name not in shown and name not in listed, (each.model, name)  # never raw
                if f"{name}_pretty" not in shown:
                    # off the page: excluded (IdempotencyRecord.response_body) or not an inline column
                    assert isinstance(each, ReadOnlyTabularInline) or name in (each.exclude or ()), name
                    continue
                seen += 1
                renderer = getattr(each, f"{name}_pretty")
                row = each.model()
                setattr(row, name, {"refresh_token": "MARKER-VALUE", "order_id": 5})
                html = str(renderer(row))
                assert "MARKER-VALUE" not in html and MASK in html and "order_id" in html
    assert seen >= 15  # outbox, jobs, provider state, webhooks, audit, payments, campaigns, events …
