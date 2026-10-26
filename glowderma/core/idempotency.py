import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from hmac import compare_digest
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import UploadedFile
from django.db import DatabaseError, connection, transaction
from django.utils.crypto import salted_hmac
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.core import timeutils
from glowderma.core.api.exceptions import IdempotencyKeyRequired, IdempotencyKeyReused, RequestInProgress
from glowderma.core.models import IdempotencyRecord

logger = logging.getLogger(__name__)


def request_fingerprint(request: Request) -> str:
    """HMAC-SHA256 (keyed by ``SECRET_KEY``) of the canonical body and query string.

    Keyed, because a body can carry secrets (``me/password/`` holds both passwords) and the stored hash lives
    for 24 h: a plain SHA-256 would let a database or backup reader test password guesses offline. The query
    string is part of it, so a key reused on ``DELETE …?product_id=`` with other ids is a reuse (422).
    """
    canonical = _canonical_body(request)
    query = sorted((key, sorted(request.query_params.getlist(key))) for key in request.query_params)
    message = json.dumps({"body": canonical, "query": query}, sort_keys=True, separators=(",", ":"))
    return salted_hmac(_FINGERPRINT_SALT, message, algorithm="sha256").hexdigest()


def _legacy_fingerprint(request: Request) -> str:
    """The unkeyed SHA-256 of the body that records written before the HMAC fingerprint carry (they expire
    within ``IDEMPOTENCY_TTL``); computed only to compare, never stored."""
    return hashlib.sha256(_canonical_body(request).encode("utf-8", "surrogatepass")).hexdigest()


_FINGERPRINT_SALT = "glowderma.core.idempotency.request_fingerprint"


def _canonical_body(request: Request) -> str:
    parsed: Any = request.data
    if (request.content_type or "").startswith("multipart/"):
        fields = {
            key: [value for value in parsed.getlist(key) if not isinstance(value, UploadedFile)]
            for key in sorted(parsed)
        }
        fields = {key: values for key, values in fields.items() if values}
        files: dict[str, list[str]] = {}
        for key in sorted(request.FILES):
            files[key] = []
            for upload in request.FILES.getlist(key):
                position = upload.tell()
                digest = hashlib.sha256()
                try:
                    upload.seek(0)
                    for chunk in upload.chunks():
                        digest.update(chunk)
                finally:
                    upload.seek(position)
                files[key].append(digest.hexdigest())
        parsed = {"fields": fields, "files": files}
    elif hasattr(parsed, "lists"):
        parsed = dict(parsed.lists())
    # The legacy form (ensure_ascii=False). core.api.parsers.JSONParser already refuses lone surrogate escapes
    # ("\ud800") with a 400; on another parser the fingerprint's ASCII re-encoding and the legacy
    # "surrogatepass" still give a hash instead of a UnicodeEncodeError (500).
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_REFRESH_ATTR = "_gd_refresh_after_commit"


def refresh_after_commit(response: Response, build: Callable[[], Any]) -> Response:
    """Mark a 2xx ``response`` whose body is rebuilt by ``build()`` once the request's transaction committed.

    For a view whose effect continues in on-commit callbacks within the same request (an eager Celery task in
    local settings): :func:`idempotent` exits its transaction (the callbacks run), then replaces the body
    with ``build()`` — a fresh read — and stores that body as the key's replay body too. Inside an outer
    transaction (nothing committed yet) the body built by the view stays. Never fakes a state: with a real
    worker the fresh read still shows what is stored.
    """
    setattr(response, _REFRESH_ATTR, build)
    return response


def _refresh(response: Response, record_id: int | None) -> Response:
    build = getattr(response, _REFRESH_ATTR, None)
    if build is None or connection.in_atomic_block or not 200 <= response.status_code < 300:
        return response
    response.data = build()
    if record_id is not None:
        IdempotencyRecord.objects.filter(pk=record_id).update(response_body=response.data)
    return response


_held = threading.local()


def _held_scopes() -> set[str]:
    scopes: set[str] | None = getattr(_held, "scopes", None)
    if scopes is None:
        scopes = _held.scopes = set()
    return scopes


@contextmanager
def _key_lock(lock_scope: str) -> Iterator[None]:
    """Hold the key from the first query until the response body is final.

    A session-level advisory lock, so it outlives the request's transaction: the on-commit callbacks (an eager
    send) and the :func:`refresh_after_commit` UPDATE of the stored body run while a concurrent replay still
    answers 409 ``request_in_progress`` instead of reading the pre-refresh body. Advisory locks are re-entrant
    within one session, so the same connection (= thread) re-entering the key is refused in process.
    """
    held = _held_scopes()
    if lock_scope in held:
        raise RequestInProgress()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [lock_scope])
        acquired = cursor.fetchone()
    if not acquired or not acquired[0]:
        raise RequestInProgress()
    held.add(lock_scope)
    try:
        yield
    finally:
        held.discard(lock_scope)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [lock_scope])
        except DatabaseError:
            # The session lock dies with the session: drop a connection we could not unlock on.
            logger.warning("could not release the idempotency key lock", exc_info=True)
            if not connection.in_atomic_block:
                connection.close()


def _parse_key(request: Request) -> uuid.UUID:
    raw_key = request.headers.get("Idempotency-Key", "")
    key = uuid.UUID(raw_key)
    if len(raw_key) != 36 or raw_key.lower() != str(key):
        raise ValueError("Idempotency-Key must be a standard 36-character UUID")
    return key


def idempotency_key(request: Request) -> uuid.UUID | None:
    """The request's well-formed ``Idempotency-Key`` (a standard 36-character UUID), else ``None`` — what
    :func:`idempotent` accepts. For work a view does *before* the decorated call (an external read), so a
    request the decorator will refuse spends nothing."""
    try:
        return _parse_key(request)
    except ValueError:
        return None


def already_answered(request: Request) -> bool:
    """A live stored response exists for this request's key and scope: :func:`idempotent` will replay it
    (or refuse a reused key) without running the view, so pre-call work can be skipped. No lock: a race only
    means the pre-call work runs once more."""
    key = idempotency_key(request)
    if key is None or not request.user.is_authenticated or not isinstance(request.auth, dict):
        return False
    return IdempotencyRecord.objects.filter(
        audience=request.auth.get("aud", ""),
        actor_id=request.user.pk,
        method=request.method,
        path=request.path_info,
        key=key,
        expires_at__gt=timeutils.now(),
    ).exists()


def idempotent(required: bool = True) -> Callable[[Callable[..., Response]], Callable[..., Response]]:
    def decorate(view: Callable[..., Response]) -> Callable[..., Response]:
        @wraps(view)
        def wrapped(self: Any, request: Request, *args: Any, **kwargs: Any) -> Response:
            if not request.user.is_authenticated or request.user.pk is None:
                raise ImproperlyConfigured("Idempotency requires an authenticated actor")
            try:
                key = _parse_key(request)
            except ValueError as exc:
                if required:
                    raise IdempotencyKeyRequired() from exc
                return _refresh(view(self, request, *args, **kwargs), None)
            if not isinstance(request.auth, dict) or not request.auth.get("aud"):
                raise ImproperlyConfigured("Idempotency requires an authenticated audience")
            scope = {
                "audience": request.auth["aud"],
                "actor_id": request.user.pk,
                "method": request.method,
                "path": request.path_info,
                "key": key,
            }
            lock_scope = f"{scope['audience']}|{scope['actor_id']}|{scope['method']}|{scope['path']}|{key}"
            record_id: int | None = None
            with _key_lock(lock_scope):
                with transaction.atomic():
                    # The transaction-level lock (re-entrant for this session) keeps the key held until an
                    # outer transaction commits, after the session lock above was released.
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", [lock_scope]
                        )
                        acquired = cursor.fetchone()
                    if not acquired or not acquired[0]:  # pragma: no cover - the session lock is held
                        raise RequestInProgress()
                    fingerprint = request_fingerprint(request)
                    record = IdempotencyRecord.objects.filter(**scope).first()
                    if record is not None and record.expires_at <= timeutils.now():
                        record.delete()
                        record = None
                    if record is not None:
                        if record.request_hash != fingerprint and not compare_digest(
                            record.request_hash, _legacy_fingerprint(request)
                        ):
                            raise IdempotencyKeyReused()
                        return Response(
                            record.response_body,
                            status=record.response_status,
                            headers={"Idempotent-Replayed": "true"},
                        )
                    response = view(self, request, *args, **kwargs)
                    if 200 <= response.status_code < 300:
                        record_id = IdempotencyRecord.objects.create(
                            **scope,
                            request_hash=fingerprint,
                            response_status=response.status_code,
                            response_body=response.data,
                        ).pk
                    else:
                        transaction.set_rollback(True)
                return _refresh(response, record_id)

        return wrapped

    return decorate
