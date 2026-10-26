"""The customer error envelope (customer contract §1.6, §5.0) and the unchanged admin envelope."""

import json
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
from django.conf import settings
from django.http import Http404
from django.test import RequestFactory
from rest_framework import exceptions, serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.core import i18n
from glowderma.core.api import exceptions as api_exceptions
from glowderma.core.api.exceptions import (
    AuthFailed,
    Conflict,
    CustomerError,
    InvalidTransition,
    NotCancellable,
    OtpLocked,
    OutOfStock,
    field_error,
    localize_field_message,
    not_cancellable,
    not_found_view,
)
from glowderma.core.api.parsers import JSONParser

CONTRACT = Path(settings.BASE_DIR) / "docs/api/customer-api-v1.md"


class NameSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(min_length=2, max_length=5)
    password = serializers.CharField(required=False)

    def validate_password(self, value: str) -> str:
        raise serializers.ValidationError(
            [field_error("password_too_short", message_key="field.password_too_short", limit=8)]
        )


class RaisingView(APIView):
    """``audience`` decides the envelope; ``exc`` is raised by every request."""

    authentication_classes: ClassVar[list[Any]] = []
    permission_classes = [AllowAny]
    parser_classes = [JSONParser]
    exc: Exception | None = None

    def post(self, request: Request) -> Response:
        if self.exc is not None:
            raise self.exc
        serializer = NameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({})


class CustomerView(RaisingView):
    audience = "customer"


def call(view: type[APIView], data: Any = None, *, lang: str = "", **extra: Any) -> Response:
    headers = {"HTTP_ACCEPT_LANGUAGE": lang} if lang else {}
    request = APIRequestFactory().post("/api/v1/x/", data or {}, format="json", **headers, **extra)
    request.request_id = "req-1"  # type: ignore[attr-defined]
    response = view.as_view()(request)
    response.render()
    return response


def test_customer_error_keys_params_details_and_retry_after() -> None:
    exc = OtpLocked(details={"retry_after": 120}, params={"m": 2}, retry_after=120)
    assert (exc.status_code, exc.get_codes(), exc.key) == (429, "otp_locked", "error.otp_locked")
    assert (
        exc.wait == 120
        and str(exc.detail) == "Urinishlar soni tugadi. 2 daqiqadan so‘ng qayta urinib ko‘ring."
    )
    generic = CustomerError("conflict", 409)
    assert generic.key == "error.conflict" and generic.details is None
    assert OutOfStock(details={"lines": []}).key == "error.OUT_OF_STOCK"
    assert OutOfStock(details={"product_id": 4, "available": 0}, one=True).key == "error.out_of_stock_one"
    assert not_cancellable("order").key == "error.order_not_cancellable"
    assert not_cancellable("appointment").key == "error.appointment_not_cancellable"
    assert not_cancellable().key == "error.not_cancellable"
    with pytest.raises(KeyError):
        OtpLocked()  # {m} is required: a programming error shows up where it is raised
    with pytest.raises(ValueError, match="catalog key"):
        CustomerError("made_up", 409)
    with pytest.raises(ValueError, match="needs a code"):
        CustomerError()


def contract_codes() -> set[str]:
    """Every code of the customer contract's §1.6 table ("Codes added for this audience")."""
    text = CONTRACT.read_text()
    table = text[text.index("Codes added for this audience") : text.index("### 1.7 Idempotency")]
    rows = re.findall(r"^\| \d{3} \| (.+?) \|", table, re.MULTILINE)
    return {code for row in rows for code in re.findall(r"`(\w+)`", row)}


def test_every_customer_code_of_the_contract_has_a_catalog_key() -> None:
    """§5.0: "a contract test maps every code a customer view can raise to an existing key"."""
    codes = contract_codes()
    assert {"device_required", "OUT_OF_STOCK", "otp_network_limit", "online_sales_unavailable"} <= codes
    overrides = {"not_cancellable": "error.not_cancellable", "OUT_OF_STOCK": "error.OUT_OF_STOCK"}
    for code in codes:
        assert overrides.get(code, f"error.{code}") in i18n.CATALOG, code
    reused = {
        "validation_error",
        "not_authenticated",
        "SESSION_EXPIRED",
        "SESSION_REPLACED",
        "invalid_credentials",
    }
    reused |= {"not_found", "throttled", "idempotency_key_required", "idempotency_key_reused"}
    reused |= {
        "request_in_progress",
        "provider_unavailable",
        "server_error",
        "conflict",
        "invalid_transition",
    }
    assert all(f"error.{code}" in i18n.CATALOG for code in reused)
    for subclass in CustomerError.__subclasses__():
        key = subclass.default_key or f"error.{subclass.error_code}"
        assert key in i18n.CATALOG, subclass.__name__


def test_customer_view_gets_details_and_localized_messages() -> None:
    CustomerView.exc = OtpLocked(details={"retry_after": 90}, params={"m": 2}, retry_after=90)
    response = call(CustomerView, lang="ru")
    assert response.status_code == 429
    assert response["Retry-After"] == "90"
    assert json.loads(response.content) == {
        "code": "otp_locked",
        "message": "Попытки закончились. Повторите через 2 мин.",
        "errors": [],
        "details": {"retry_after": 90},
        "request_id": "req-1",
    }
    CustomerView.exc = CustomerError("delivery_unavailable", 409, key="error.delivery_not_offered")
    assert (
        call(CustomerView, lang="en").data["message"]
        == "Delivery is not available right now. Please choose pickup."
    )


@pytest.mark.parametrize(
    ("exc", "code", "status", "message"),
    [
        (
            NotCancellable("Bu buyurtmani bekor qilib bo‘lmaydi."),
            "not_cancellable",
            409,
            "This cannot be cancelled.",
        ),
        (
            InvalidTransition("x"),
            "invalid_transition",
            409,
            "This action is not available right now. Please refresh.",
        ),
        (Conflict("x"), "conflict", 409, "The action could not be completed. Please refresh."),
        (
            AuthFailed("SESSION_REPLACED"),
            "SESSION_REPLACED",
            401,
            "Your account was signed in on another device.",
        ),
        (exceptions.NotFound(), "not_found", 404, "Not found."),
        (Http404(), "not_found", 404, "Not found."),
        (exceptions.Throttled(wait=7), "throttled", 429, "Too many attempts. Try again in 7 s."),
        (exceptions.Throttled(), "throttled", 429, "Too many attempts. Try again in 60 s."),
        (exceptions.MethodNotAllowed("PUT"), "method_not_allowed", 405, "This action is not supported."),
        (
            exceptions.UnsupportedMediaType("text/plain"),
            "unsupported_media_type",
            415,
            "Unsupported data format.",
        ),
        (exceptions.NotAuthenticated(), "not_authenticated", 401, "Please sign in."),
        (exceptions.PermissionDenied(), "permission_denied", 403, "Access denied."),
        (RuntimeError("boom"), "server_error", 500, "Unexpected error."),
    ],
)
def test_reused_and_framework_errors_map_to_catalog_keys(
    exc: Exception, code: str, status: int, message: str
) -> None:
    CustomerView.exc = exc
    response = call(CustomerView, lang="en")
    assert (response.status_code, response.data["code"], response.data["message"]) == (status, code, message)
    assert response.data["details"] is None


def test_customer_field_errors_are_localized() -> None:
    CustomerView.exc = None
    response = call(CustomerView, {"name": "x", "password": "short"}, lang="ru")
    assert response.status_code == 400
    assert response.data["code"] == "validation_error" and response.data["message"] == "Проверьте поля."
    assert response.data["details"] is None
    assert response.data["errors"] == [
        {"field": "name", "code": "min_length", "message": "Минимум 2 символов."},
        {
            "field": "password",
            "code": "password_too_short",
            "message": "Пароль должен быть не короче 8 символов.",
        },
    ]
    missing = call(CustomerView, {}, lang="en")
    assert missing.data["errors"] == [
        {"field": "name", "code": "required", "message": "This field is required."}
    ]


def test_language_falls_back_to_uzbek() -> None:
    CustomerView.exc = None
    assert call(CustomerView, {}).data["errors"][0]["message"] == "Majburiy maydon."


def test_localize_field_message_prefers_the_code_key_and_keeps_unknown_texts() -> None:
    assert localize_field_message("required", "Majburiy maydon.", "ru") == "Обязательное поле."
    assert localize_field_message("null", "Majburiy maydon.", "en") == "This field is required."
    assert localize_field_message("invalid", "Telefon raqamini to‘g‘ri kiriting.", "en") == (
        "Please enter a valid phone number."
    )
    assert localize_field_message(
        "otp_invalid", "Kod noto‘g‘ri. Yana 3 marta urinishingiz mumkin.", "ru"
    ) == ("Неверный код. Осталось попыток: 3.")
    assert localize_field_message("invalid", "Serializer-specific text.", "ru") == "Serializer-specific text."


def test_field_error_message_key_rules() -> None:
    detail = field_error("latlng_pair", message_key="field.latlng_pair")
    assert (str(detail), detail.code) == ("Joylashuvni qayta tanlang.", "latlng_pair")
    assert (
        str(field_error("invalid", message_key="field.phone_invalid")) == "Telefon raqamini to‘g‘ri kiriting."
    )
    with pytest.raises(ValueError, match="catalog key"):
        field_error("invalid", message_key="error.not_found")
    with pytest.raises(ValueError, match="either"):
        field_error("invalid", "x", message_key="field.phone_invalid")
    with pytest.raises(ValueError, match="explicit message"):
        field_error("password_incorrect")


# --- the admin envelope stays byte for byte ---


def test_admin_validation_error_body_is_unchanged() -> None:
    RaisingView.exc = None
    response = call(RaisingView, {"name": "x"}, lang="ru")
    assert response.content == (
        b'{"code":"validation_error","message":"Maydonlarni tekshiring.","errors":[{"field":"name",'
        b'"code":"min_length","message":"Kamida 2 ta belgi kiriting."}],"request_id":"req-1"}'
    )
    assert not response.has_header("Content-Language")


def test_admin_conflict_body_is_unchanged() -> None:
    RaisingView.exc = Conflict("Bu Billz ID bilan mahsulot allaqachon bor.")
    response = call(RaisingView, lang="en")
    assert response.status_code == 409
    assert response.content == (
        b'{"code":"conflict","message":"Bu Billz ID bilan mahsulot allaqachon bor.","errors":[],'
        b'"request_id":"req-1"}'
    )


def test_admin_customer_error_outside_a_customer_view_reads_uzbek() -> None:
    RaisingView.exc = CustomerError("conflict", 409)
    response = call(RaisingView, lang="en")
    assert response.data == {
        "code": "conflict",
        "message": "Amalni bajarib bo‘lmadi. Ma’lumotlarni yangilang.",
        "errors": [],
        "request_id": "req-1",
    }


def test_not_found_page_is_customer_shaped_only_on_customer_paths() -> None:
    factory = RequestFactory()
    customer = not_found_view(factory.get("/api/v1/catalog/nope/", HTTP_ACCEPT_LANGUAGE="ru"), Http404())
    assert customer.status_code == 404 and customer["Content-Language"] == "ru"
    assert json.loads(customer.content) == {
        "code": "not_found",
        "message": "Не найдено.",
        "errors": [],
        "details": None,
        "request_id": "",
    }
    admin = not_found_view(factory.get("/api/v1/admin/nope/", HTTP_ACCEPT_LANGUAGE="ru"), Http404())
    assert json.loads(admin.content) == {
        "code": "not_found",
        "message": "Topilmadi.",
        "errors": [],
        "request_id": "",
    }
    assert api_exceptions.is_customer_path("/api/v1/me/") and not api_exceptions.is_customer_path(
        "/api/v1/health/"
    )


def test_server_error_page_is_customer_shaped_on_customer_paths() -> None:
    page = api_exceptions.server_error_view(RequestFactory().get("/api/v1/cart/", HTTP_ACCEPT_LANGUAGE="en"))
    assert page.status_code == 500 and json.loads(page.content)["message"] == "Unexpected error."
    admin = api_exceptions.server_error_view(RequestFactory().get("/api/v1/admin/orders/"))
    assert "details" not in json.loads(admin.content)


def test_unknown_customer_code_falls_back_to_the_status_key(caplog: pytest.LogCaptureFixture) -> None:
    body = api_exceptions.customer_error_body("odd_code", None, lang="en", status=409)
    assert body["message"] == i18n.t("error.conflict", "en")
    assert "No catalog key" in caplog.text
