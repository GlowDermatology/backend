from typing import Any, cast

from django.test import Client, override_settings
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView


@override_settings(DEBUG=False)
def test_unknown_route_returns_404_envelope() -> None:
    response = Client().get("/api/unknown/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["request_id"] == response["X-Request-ID"]
    assert set(response.json()) == {"code", "message", "errors", "request_id"}


@override_settings(INTERNAL_API_SECRET="test-internal-secret-at-least-32-characters")  # noqa: S106
def test_request_id_trusted_only_with_secret() -> None:
    client = Client()
    headers: dict[str, Any] = {"HTTP_X_REQUEST_ID": "trusted-1234"}
    assert client.get("/api/v1/health/", **headers)["X-Request-ID"] != "trusted-1234"
    headers["HTTP_X_GD_INTERNAL_SECRET"] = "test-internal-secret-at-least-32-characters"  # noqa: S105
    assert client.get("/api/v1/health/", **headers)["X-Request-ID"] == "trusted-1234"
    headers["HTTP_X_REQUEST_ID"] = "bad!"
    assert client.get("/api/v1/health/", **headers)["X-Request-ID"] != "bad!"


def test_api_responses_are_no_store() -> None:
    response = Client().get("/api/v1/health/")
    assert response["Cache-Control"] == "no-store"
    assert response["Content-Language"] == "uz"


def test_payload_too_large_413() -> None:
    response = Client().post(
        "/api/v1/health/", data=b"x", content_type="application/json", CONTENT_LENGTH=str(6 * 1024 * 1024 + 1)
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_multipart_under_six_mib_is_accepted() -> None:
    class UploadView(APIView):
        authentication_classes = []
        permission_classes = [AllowAny]

        def post(self, request: Request) -> Response:
            return Response({"length": len(cast(dict[str, str], request.data)["text"])})

    response = UploadView.as_view()(
        APIRequestFactory().post("/api/upload/", {"text": "x" * (3 * 1024 * 1024)})
    )
    assert response.status_code == 200
