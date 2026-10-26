from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Thread
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.core import audit, timeutils
from glowderma.core.idempotency import idempotent, refresh_after_commit
from glowderma.core.models import AuditEntry, IdempotencyRecord

pytestmark = pytest.mark.django_db


@dataclass
class Actor:
    pk: int = 7
    is_authenticated: bool = True
    full_name: str = "Test administrator"

    def get_username(self) -> str:
        return "admin@example.uz"


class StubAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[Actor, dict[str, str]]:
        return Actor(), {"aud": "staff"}


class WriteView(APIView):
    authentication_classes = [StubAuthentication]
    permission_classes = [AllowAny]
    entered: Event | None = None
    release: Event | None = None

    def write(self, request: Request) -> Response:
        data = cast(dict[str, Any], request.data)
        upload = request.FILES.get("file")
        file_text = upload.read().decode() if upload is not None else None
        entry = audit.record(
            action="test.idempotent_write",
            entity_type="test",
            entity_id=request.path_info,
            actor=request.user,
            after={"value": data.get("value"), "file": file_text},
        )
        if data.get("raise_error"):
            raise ValidationError({"value": "Rejected after effect"})
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(timeout=10):
            raise TimeoutError("Concurrent test did not release the first request")
        return Response({"audit_id": entry.pk, "file": file_text}, status=int(data.get("status", 201)))

    @idempotent()
    def post(self, request: Request) -> Response:
        return self.write(request)


class OptionalWriteView(WriteView):
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        return self.write(request)


def post(
    key: str | None,
    body: dict[str, Any] | None = None,
    *,
    path: str = "/api/test/1/",
    view: type[WriteView] = WriteView,
    entered: Event | None = None,
    release: Event | None = None,
    multipart: bool = False,
) -> Response:
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}
    request = APIRequestFactory().post(
        path,
        body if body is not None else {"value": "one"},
        format="multipart" if multipart else "json",
        **headers,
    )
    return view.as_view(entered=entered, release=release)(request)


def test_same_key_same_body_replays_once() -> None:
    key = str(uuid4())
    first = post(key, {"value": "one", "extra": {"a": 1, "b": 2}})
    replay = post(key, {"extra": {"b": 2, "a": 1}, "value": "one"})
    assert first.status_code == replay.status_code == 201
    assert replay.data == first.data
    assert replay["Idempotent-Replayed"] == "true"
    assert not first.has_header("Idempotent-Replayed")
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 1


def test_same_key_different_body_422() -> None:
    key = str(uuid4())
    assert post(key, {"value": "one"}).status_code == 201
    response = post(key, {"value": "two"})
    assert response.status_code == 422
    assert response.data["code"] == "idempotency_key_reused"
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_409() -> None:
    key = str(uuid4())
    entered, release = Event(), Event()
    results: list[Response] = []
    errors: list[BaseException] = []

    def first_request() -> None:
        try:
            results.append(post(key, entered=entered, release=release))
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    worker = Thread(target=first_request, daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=10), "First request never entered its view"
        # Measure advisory-lock contention independently of connection establishment.
        connection.ensure_connection()
        started = perf_counter()
        duplicate = post(key)
        elapsed = perf_counter() - started
        assert elapsed < 1, f"Duplicate request blocked for {elapsed:.3f} seconds"
        assert duplicate.status_code == 409
        assert duplicate.data["code"] == "request_in_progress"
        assert duplicate["Retry-After"] == "1"
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1
    assert results[0].status_code == 201
    replay = post(key)
    assert replay.status_code == 201
    assert replay.data == results[0].data
    assert replay["Idempotent-Replayed"] == "true"
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 1


def test_non_2xx_not_stored() -> None:
    key = str(uuid4())
    assert post(key, {"status": 400}).status_code == 400
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 0
    assert post(key, {"status": 400}).status_code == 400
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 0
    assert post(key, {"value": "corrected"}).status_code == 201
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 1


def test_exception_rolls_back_effect_and_record() -> None:
    response = post(str(uuid4()), {"raise_error": True})
    assert response.status_code == 400
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 0


def test_form_fingerprint_preserves_every_value() -> None:
    key = str(uuid4())
    factory = APIRequestFactory()
    first = factory.post(
        "/api/test/",
        "value=one&value=last",
        content_type="application/x-www-form-urlencoded",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    second = factory.post(
        "/api/test/",
        "value=two&value=last",
        content_type="application/x-www-form-urlencoded",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    assert WriteView.as_view()(first).status_code == 201
    assert WriteView.as_view()(second).status_code == 422


def test_same_key_different_path_is_separate() -> None:
    key = str(uuid4())
    first = post(key, path="/api/test/1/")
    second = post(key, path="/api/test/2/")
    assert first.status_code == second.status_code == 201
    assert first.data["audit_id"] != second.data["audit_id"]
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 2


def test_expired_key_counts_as_new(monkeypatch: pytest.MonkeyPatch) -> None:
    key = str(uuid4())
    initial_now = timeutils.now()
    monkeypatch.setattr(timeutils, "now", lambda: initial_now)
    first = post(key)
    record = IdempotencyRecord.objects.get()
    assert record.expires_at == initial_now + timedelta(hours=24)
    monkeypatch.setattr(timeutils, "now", lambda: initial_now + timedelta(hours=24, seconds=1))
    second = post(key, {"value": "new"})
    assert first.status_code == second.status_code == 201
    assert first.data["audit_id"] != second.data["audit_id"]
    assert not second.has_header("Idempotent-Replayed")
    assert AuditEntry.objects.count() == 2
    assert IdempotencyRecord.objects.count() == 1


def test_multipart_same_size_different_file_422() -> None:
    key = str(uuid4())
    first_file = SimpleUploadedFile("proof.txt", b"aaaa", content_type="text/plain")
    second_file = SimpleUploadedFile("proof.txt", b"bbbb", content_type="text/plain")
    first = post(key, {"value": "one", "file": first_file}, multipart=True)
    second = post(key, {"value": "one", "file": second_file}, multipart=True)
    assert first.status_code == 201
    assert second.status_code == 422
    assert second.data["code"] == "idempotency_key_reused"
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 1


def test_multipart_fingerprinting_preserves_file_for_view() -> None:
    upload = SimpleUploadedFile("proof.txt", b"file body", content_type="text/plain")
    response = post(str(uuid4()), {"file": upload}, multipart=True)
    assert response.status_code == 201
    assert response.data["file"] == "file body"


@pytest.mark.parametrize(
    "key",
    [
        None,
        "not-a-uuid",
        "12345678123456781234567812345678",
        "{12345678-1234-5678-1234-567812345678}",
        "urn:uuid:12345678-1234-5678-1234-567812345678",
    ],
)
def test_missing_key_400_when_required(key: str | None) -> None:
    response = post(key)
    assert response.status_code == 400
    assert response.data["code"] == "idempotency_key_required"
    assert AuditEntry.objects.count() == IdempotencyRecord.objects.count() == 0


@pytest.mark.parametrize("key", [None, "not-a-uuid"])
def test_optional_mode_without_key_runs_normally(key: str | None) -> None:
    assert post(key, view=OptionalWriteView).status_code == 201
    assert post(key, view=OptionalWriteView).status_code == 201
    assert AuditEntry.objects.count() == 2
    assert IdempotencyRecord.objects.count() == 0


def test_a_lone_surrogate_in_the_body_is_a_parse_error_before_any_effect() -> None:
    """The strict JSON parser answers 400 before the fingerprint, the effect or the stored record."""
    key = str(uuid4())
    request = APIRequestFactory().post(
        "/api/test/", '{"value": "\\ud800"}', content_type="application/json", HTTP_IDEMPOTENCY_KEY=key
    )
    response = WriteView.as_view()(request)
    assert response.status_code == 400
    assert response.data["code"] == "parse_error"
    assert not AuditEntry.objects.filter(action="test.idempotent_write").exists()
    assert not IdempotencyRecord.objects.filter(key=key).exists()


# --- refresh_after_commit -----------------------------------------------------------------------------------

_STORE: dict[str, str] = {}


class AfterCommitView(APIView):
    """A write whose effect finishes in an on-commit callback (like an eager Celery send)."""

    authentication_classes = [StubAuthentication]
    permission_classes = [AllowAny]

    def write(self, request: Request) -> Response:
        _STORE["state"] = "queued"
        transaction.on_commit(lambda: _STORE.update(state="sent"))

        def body() -> dict[str, str]:
            return {"state": _STORE["state"]}

        return refresh_after_commit(Response(body(), status=201), body)

    @idempotent()
    def post(self, request: Request) -> Response:
        return self.write(request)


class OptionalAfterCommitView(AfterCommitView):
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        return self.write(request)


def _after_commit_post(view: type[APIView], key: str | None) -> Response:
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}
    request = APIRequestFactory().post("/api/test/after-commit/", {}, format="json", **headers)
    response: Response = view.as_view()(request)
    return response


@pytest.mark.django_db(transaction=True)
def test_refresh_after_commit_rebuilds_the_body_and_the_replay() -> None:
    key = str(uuid4())
    first = _after_commit_post(AfterCommitView, key)
    assert first.status_code == 201 and first.data == {"state": "sent"}
    assert IdempotencyRecord.objects.get().response_body == {"state": "sent"}
    replay = _after_commit_post(AfterCommitView, key)
    assert replay["Idempotent-Replayed"] == "true" and replay.data == {"state": "sent"}
    assert _after_commit_post(OptionalAfterCommitView, None).data == {"state": "sent"}


def test_refresh_after_commit_keeps_the_body_inside_an_outer_transaction() -> None:
    first = _after_commit_post(AfterCommitView, str(uuid4()))
    assert first.data == {"state": "queued"}  # nothing committed yet: the body is the stored state
    assert IdempotencyRecord.objects.get().response_body == {"state": "queued"}


class RetryDuringSendView(APIView):
    """An eager on-commit send during which the client retries the same key, from another connection and
    from the same one (a nested call)."""

    authentication_classes = [StubAuthentication]
    permission_classes = [AllowAny]
    seen: list[tuple[str, Any]] = []

    @idempotent()
    def post(self, request: Request) -> Response:
        _STORE["state"] = "queued"
        key = request.headers["Idempotency-Key"]

        def finish() -> None:
            _STORE["state"] = "sent"
            other: list[Response] = []

            def retry() -> None:
                try:
                    other.append(_retry_post(key))
                finally:
                    connection.close()

            worker = Thread(target=retry, daemon=True)
            worker.start()
            worker.join(timeout=10)
            self.seen.append(("other connection", other[0].data if other else "timeout"))
            self.seen.append(("same connection", _retry_post(key).data))

        transaction.on_commit(finish)

        def body() -> dict[str, str]:
            return {"state": _STORE["state"]}

        return refresh_after_commit(Response(body(), status=201), body)


def _retry_post(key: str) -> Response:
    request = APIRequestFactory().post(
        "/api/test/retry-during-send/", {}, format="json", HTTP_IDEMPOTENCY_KEY=key
    )
    response: Response = RetryDuringSendView.as_view()(request)
    return response


@pytest.mark.django_db(transaction=True)
def test_a_retry_while_the_body_is_refreshed_is_in_progress_never_the_pre_send_body() -> None:
    """The key stays held across the on-commit callbacks and the stored-body refresh: a retry racing the
    first response answers 409 ``request_in_progress``; once the response is out, a replay is that body."""
    RetryDuringSendView.seen = []
    key = str(uuid4())
    first = _retry_post(key)
    assert first.status_code == 201 and first.data == {"state": "sent"}
    assert [label for label, _ in RetryDuringSendView.seen] == ["other connection", "same connection"]
    for _, data in RetryDuringSendView.seen:
        assert data["code"] == "request_in_progress"
    replay = _retry_post(key)
    assert replay["Idempotent-Replayed"] == "true" and replay.data == {"state": "sent"}
    with connection.cursor() as cursor:  # the session lock was released
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        assert cursor.fetchone() == (0,)
