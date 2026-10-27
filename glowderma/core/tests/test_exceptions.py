from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from django.core.exceptions import BadRequest, RequestDataTooBig, TooManyFieldsSent, TooManyFilesSent
from django.test import RequestFactory
from rest_framework import serializers
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    ErrorDetail,
    MethodNotAllowed,
    NotAcceptable,
    NotAuthenticated,
    ParseError,
    Throttled,
    UnsupportedMediaType,
)
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.core.api.exceptions import (
    ENDPOINT_FIELD_CODES,
    FIELD_MESSAGES,
    MESSAGES,
    AuthFailed,
    Conflict,
    InvalidTransition,
    NotCancellable,
    OAuthExchangeFailed,
    ProviderUnavailable,
    RequestInProgress,
    envelope_exception_handler,
    field_error,
    flatten_errors,
    server_error_view,
)


class HeaderAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> None:
        return None

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="test"'


def exception_response(exc: Exception, *, with_authenticator: bool = False) -> Response:
    class RaisingView(APIView):
        authentication_classes = [HeaderAuthentication] if with_authenticator else []
        permission_classes = [AllowAny]

        def get(self, request: Request) -> Response:
            raise exc

    return RaisingView.as_view()(APIRequestFactory().get("/api/test/"))


class TierSerializer(serializers.Serializer[dict[str, Any]]):
    percent = serializers.DecimalField(max_digits=5, decimal_places=2)


class TiersSerializer(serializers.Serializer[dict[str, Any]]):
    tiers = TierSerializer(many=True)


class NameSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(min_length=2)


class TextsSerializer(serializers.Serializer[dict[str, Any]]):
    uz = NameSerializer()


class ProductSerializer(serializers.Serializer[dict[str, Any]]):
    texts = TextsSerializer()


def validation_response(serializer: serializers.Serializer[Any]) -> Response:
    with pytest.raises(serializers.ValidationError) as caught:
        serializer.is_valid(raise_exception=True)
    return exception_response(caught.value)


def test_nested_list_errors_flatten() -> None:
    response = validation_response(TiersSerializer(data={"tiers": [{"percent": "1.00"}, {"percent": "bad"}]}))
    assert response.status_code == 400
    assert response.data["code"] == "validation_error"
    assert response.data["message"] == "Maydonlarni tekshiring."
    assert response.data["errors"][0]["field"] == "tiers.1.percent"
    assert response.data["errors"][0]["code"] == "invalid"


def test_nested_dict_errors_flatten() -> None:
    response = validation_response(ProductSerializer(data={"texts": {"uz": {"name": "x"}}}))
    assert response.data["errors"][0]["field"] == "texts.uz.name"
    assert response.data["errors"][0]["code"] == "min_length"


def test_non_field_error_has_null_field() -> None:
    response = exception_response(
        serializers.ValidationError(
            {"non_field_errors": [ErrorDetail("Invalid combination", code="invalid")]}
        )
    )
    assert response.data["errors"] == [{"field": None, "code": "invalid", "message": "Invalid combination"}]


def test_validation_errors_deduplicate_field_and_code_in_order() -> None:
    response = exception_response(
        serializers.ValidationError(
            {
                "name": [
                    ErrorDetail("First", "invalid"),
                    ErrorDetail("Second", "invalid"),
                    ErrorDetail("Required", "required"),
                ]
            }
        )
    )
    assert response.data["errors"] == [
        {"field": "name", "code": "invalid", "message": "First"},
        {"field": "name", "code": "required", "message": "Required"},
    ]


def test_invalid_transition_is_409() -> None:
    response = exception_response(InvalidTransition())
    assert response.status_code == 409
    assert response.data["code"] == "invalid_transition"
    assert response.data["errors"] == []


@pytest.mark.parametrize("with_authenticator", [False, True])
def test_auth_failed_is_401_on_view_without_authenticators(with_authenticator: bool) -> None:
    response = exception_response(AuthFailed("invalid_credentials"), with_authenticator=with_authenticator)
    assert response.status_code == 401
    assert response.data["code"] == "invalid_credentials"
    assert response.data["message"] == "Login yoki parol noto‘g‘ri."
    assert response.data["errors"] == []


@pytest.mark.parametrize("exception_type", [NotAuthenticated, AuthenticationFailed])
@pytest.mark.parametrize("with_authenticator", [False, True])
def test_builtin_auth_failures_are_401(
    exception_type: type[NotAuthenticated] | type[AuthenticationFailed], with_authenticator: bool
) -> None:
    response = exception_response(exception_type(), with_authenticator=with_authenticator)
    assert response.status_code == 401
    assert response.data["code"] == "not_authenticated"
    if with_authenticator:
        assert response["WWW-Authenticate"] == 'Bearer realm="test"'


def test_throttled_has_retry_after_header_only() -> None:
    response = exception_response(Throttled(wait=3.2))
    assert response.status_code == 429
    assert response["Retry-After"] == "4"
    assert response.data["message"] == "Juda ko‘p urinish. 4 soniyadan so‘ng qayta urinib ko‘ring."
    assert set(response.data) == {"code", "message", "errors", "request_id"}
    assert response.data["errors"] == []


def test_500_hides_details(caplog: pytest.LogCaptureFixture) -> None:
    response = exception_response(RuntimeError("private database connection detail"))
    assert response.status_code == 500
    assert response.data["code"] == "server_error"
    assert response.data["message"] == "Kutilmagan xato."
    assert response.data["errors"] == []
    assert "private database connection detail" not in str(response.data)
    assert any(record.exc_info is not None for record in caplog.records)


def test_datetime_has_seconds_precision() -> None:
    class TimestampSerializer(serializers.Serializer[dict[str, Any]]):
        created_at = serializers.DateTimeField()

    value = datetime(2026, 10, 24, 7, 0, 0, 123456, tzinfo=UTC)
    assert TimestampSerializer({"created_at": value}).data["created_at"] == "2026-10-24T12:00:00+05:00"


@pytest.mark.parametrize("exception_type", [Conflict, InvalidTransition, NotCancellable, RequestInProgress])
def test_conflict_message_and_code_are_preserved(exception_type: type[APIException]) -> None:
    default = exception_type()
    response = exception_response(default)
    assert response.status_code == 409
    assert response.data["message"] == str(default.detail)
    assert response.data["code"] == default.get_codes()
    custom = exception_response(exception_type("Kategoriyada mahsulotlar bor.", code="custom_conflict"))
    assert custom.data["message"] == "Kategoriyada mahsulotlar bor."
    assert custom.data["code"] == "custom_conflict"
    if exception_type is RequestInProgress:
        assert response["Retry-After"] == "1"


def test_suspicious_request_is_400() -> None:
    response = exception_response(TooManyFieldsSent("private limits"))
    assert response.status_code == 400
    assert response.data["code"] == "bad_request"


def test_throttled_without_wait_has_no_seconds_count() -> None:
    response = exception_response(Throttled())
    assert response.status_code == 429
    assert response.data["message"] == "Juda ko‘p urinish."
    assert not response.has_header("Retry-After")


@pytest.mark.parametrize(
    "exc",
    [MethodNotAllowed("POST"), NotAcceptable(), UnsupportedMediaType("private/client-type"), ParseError()],
)
def test_framework_defaults_use_uzbek_fallback(exc: APIException) -> None:
    response = exception_response(exc)
    assert response.status_code == exc.status_code
    assert response.data["code"] == exc.default_code
    assert response.data["message"] == "Maydonlarni tekshiring."
    assert response.data["errors"] == []


@pytest.mark.parametrize(
    "exc",
    [
        MethodNotAllowed("POST", detail="custom", code="custom_code"),
        NotAcceptable("custom", code="custom_code"),
        UnsupportedMediaType("private/client-type", detail="custom", code="custom_code"),
        ParseError("custom", code="custom_code"),
    ],
)
def test_explicit_framework_exception_details_are_preserved(exc: APIException) -> None:
    response = exception_response(exc)
    assert response.data["message"] == "custom"
    assert response.data["code"] == "custom_code"


@pytest.mark.parametrize("exception_type", [TooManyFieldsSent, TooManyFilesSent])
def test_suspicious_requests_keep_django_security_logging(
    exception_type: type[TooManyFieldsSent], caplog: pytest.LogCaptureFixture
) -> None:
    response = exception_response(exception_type("limits exceeded"))
    assert response.status_code == 400
    records = [
        record for record in caplog.records if record.name == f"django.security.{exception_type.__name__}"
    ]
    assert len(records) == 1
    assert records[0].exc_info
    assert getattr(records[0], "status_code", None) == 400


@pytest.mark.parametrize(
    "exc", [RuntimeError("private"), RequestDataTooBig("private"), BadRequest("private")]
)
def test_internal_failures_use_rollback_hook(exc: Exception) -> None:
    with patch("glowderma.core.api.exceptions.set_rollback") as rollback:
        envelope_exception_handler(exc, {})
    rollback.assert_called_once()


def test_500_logs_request_metadata(caplog: pytest.LogCaptureFixture) -> None:
    request = RequestFactory().get("/api/private/")
    request.request_id = "request-1234"  # type: ignore[attr-defined]
    envelope_exception_handler(RuntimeError("private"), {"request": request})
    server_error_view(request)
    assert (
        len(
            [
                r
                for r in caplog.records
                if "request-1234" in r.getMessage() and "/api/private/" in r.getMessage()
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    ("field", "data", "code", "message"),
    [
        (serializers.CharField(), {}, "required", "Majburiy maydon."),
        (serializers.CharField(), {"value": None}, "null", "Majburiy maydon."),
        (serializers.CharField(), {"value": ""}, "blank", "Maydonni to‘ldiring."),
        (serializers.IntegerField(), {"value": "bad"}, "invalid", "Noto‘g‘ri qiymat."),
        (
            serializers.DecimalField(max_digits=5, decimal_places=2),
            {"value": "bad"},
            "invalid",
            "Noto‘g‘ri qiymat.",
        ),
        (
            serializers.ChoiceField(choices=["allowed"]),
            {"value": "bad"},
            "invalid_choice",
            "Ruxsat etilmagan qiymat.",
        ),
        (serializers.CharField(min_length=3), {"value": "x"}, "min_length", "Kamida 3 ta belgi kiriting."),
        (serializers.CharField(max_length=3), {"value": "xxxx"}, "max_length", "3 belgidan oshmasin."),
        (serializers.IntegerField(min_value=5), {"value": 4}, "min_value", "Qiymat 5 dan kam bo‘lmasin."),
        (
            serializers.DecimalField(max_digits=5, decimal_places=2, max_value=12),
            {"value": "12.01"},
            "max_value",
            "Qiymat 12 dan oshmasin.",
        ),
        (
            serializers.DecimalField(max_digits=5, decimal_places=2),
            {"value": "1.234"},
            "max_decimal_places",
            "Ko‘pi bilan 2 ta kasr raqam.",
        ),
    ],
)
def test_stock_field_messages_follow_contract(
    field: serializers.Field[Any, Any, Any, Any], data: dict[str, Any], code: str, message: str
) -> None:
    class InputSerializer(serializers.Serializer[dict[str, Any]]):
        value = field

    response = validation_response(InputSerializer(data=data))
    assert response.data["errors"] == [{"field": "value", "code": code, "message": message}]


@pytest.mark.parametrize(
    ("field", "data", "message"),
    [
        (
            serializers.CharField(error_messages={"required": "Enter the product name."}),
            {},
            "Enter the product name.",
        ),
        (
            serializers.CharField(
                min_length=3, error_messages={"min_length": "Name needs {min_length} letters."}
            ),
            {"value": "x"},
            "Name needs 3 letters.",
        ),
    ],
)
def test_explicit_field_messages_are_preserved(
    field: serializers.Field[Any, Any, Any, Any], data: dict[str, Any], message: str
) -> None:
    class InputSerializer(serializers.Serializer[dict[str, Any]]):
        value = field

    response = validation_response(InputSerializer(data=data))
    assert response.data["errors"][0]["message"] == message


def test_django_body_limit_error_is_payload_too_large() -> None:
    response = exception_response(RequestDataTooBig("internal upload limit detail"))
    assert response.status_code == 413
    assert response.data["code"] == "payload_too_large"
    assert response.data["message"] == "Fayl juda katta."
    assert response.data["errors"] == []


def test_provider_unavailable_is_503_envelope() -> None:
    """Contract 1.6: 503 `provider_unavailable` "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring."."""
    response = exception_response(ProviderUnavailable())
    assert response.status_code == 503
    assert response.data == {
        "code": "provider_unavailable",
        "message": "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring.",
        "errors": [],
        "request_id": "",
    }


def test_oauth_exchange_failed_is_400_envelope() -> None:
    """Contract 2.1: 400 `oauth_exchange_failed` (bad/expired code, verifier mismatch)."""
    response = exception_response(OAuthExchangeFailed())
    assert response.status_code == 400
    assert response.data["code"] == "oauth_exchange_failed"
    assert response.data["message"] == "Ijtimoiy tarmoq orqali kirish amalga oshmadi."
    assert response.data["errors"] == []


def test_any_api_exception_subclass_default_code_reaches_the_envelope() -> None:
    """Contract 1.6: the envelope ``code`` is the exception's ``default_code``; a project subclass raised bare
    keeps its own Uzbek ``default_detail`` (only DRF's stock English defaults are swapped)."""

    class BillzDown(APIException):
        status_code = 503
        default_code = "billz_down"
        default_detail = "Billz javob bermadi."

    response = exception_response(BillzDown())
    assert response.status_code == 503
    assert response.data["code"] == "billz_down"
    assert response.data["message"] == "Billz javob bermadi."
    assert response.data["errors"] == []
    assert exception_response(BillzDown("Billz 30 soniya javob bermadi.")).data["message"] == (
        "Billz 30 soniya javob bermadi."
    )
    # Stock DRF exceptions without a MESSAGES entry still get the generic Uzbek fallback.
    assert exception_response(NotAcceptable()).data["message"] == "Maydonlarni tekshiring."


def test_social_account_not_allowed_is_401() -> None:
    """Contract 2.1: 401 `social_account_not_allowed` "Bu hisob administrator sifatida ro‘yxatdan
    o‘tmagan."."""
    response = exception_response(AuthFailed("social_account_not_allowed"))
    assert response.status_code == 401
    assert response.data["code"] == "social_account_not_allowed"
    assert response.data["message"] == "Bu hisob administrator sifatida ro‘yxatdan o‘tmagan."
    assert MESSAGES["social_account_not_allowed"] == response.data["message"]
    with pytest.raises(ValueError, match="Unknown authentication failure code"):
        AuthFailed("made_up")


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("read_only", "Bu maydonni o‘zgartirib bo‘lmaydi."),
        ("file_too_large", "Rasm 5 MB dan oshmasin."),
        ("unsupported_image", "Faqat JPEG, PNG yoki WebP rasm."),
        ("mismatch", "Ro‘yxat joriy elementlarga mos emas."),
    ],
)
def test_generic_contract_field_codes_have_default_messages(code: str, message: str) -> None:
    """Contract 1.6 generic field codes: read_only, file_too_large, unsupported_image, max_items, mismatch."""
    assert FIELD_MESSAGES[code] == message
    detail = field_error(code)
    assert detail == message and detail.code == code
    response = exception_response(serializers.ValidationError({"slug": [detail]}))
    assert response.data["errors"] == [{"field": "slug", "code": code, "message": message}]
    # A bare code (or an empty message) also resolves to the default.
    assert flatten_errors({"slug": [ErrorDetail(code, code=code), ErrorDetail("", code=code)]}) == [
        {"field": "slug", "code": code, "message": message}
    ]


def test_unknown_field_code_is_rejected() -> None:
    """A code outside FIELD_MESSAGES / ENDPOINT_FIELD_CODES is a typo the FE mapper would never match."""
    with pytest.raises(ValueError, match="in_pastt"):
        field_error("in_pastt", "Sana o‘tib ketgan.")
    with pytest.raises(ValueError, match="made_up"):
        field_error("made_up")


def test_max_items_formats_the_limit() -> None:
    """Contract 1.6: `max_items` "Ko‘pi bilan {n} ta."."""
    detail = field_error("max_items", limit=8)
    assert detail == "Ko‘pi bilan 8 ta." and detail.code == "max_items"
    with pytest.raises(ValueError, match="limit"):
        field_error("max_items")


@pytest.mark.parametrize("code", sorted(ENDPOINT_FIELD_CODES))
def test_endpoint_specific_codes_need_an_explicit_message(code: str) -> None:
    assert code not in FIELD_MESSAGES
    with pytest.raises(ValueError, match=code):
        field_error(code)
    detail = field_error(code, "Aniq xabar.")
    response = exception_response(serializers.ValidationError({"tiers": [{"percent": [detail]}]}))
    assert response.data["errors"] == [{"field": "tiers.0.percent", "code": code, "message": "Aniq xabar."}]
