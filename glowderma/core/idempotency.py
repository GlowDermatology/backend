import hashlib
import json
import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import UploadedFile
from django.db import connection, transaction
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.core import timeutils
from glowderma.core.api.exceptions import IdempotencyKeyRequired, IdempotencyKeyReused, RequestInProgress
from glowderma.core.models import IdempotencyRecord


def request_fingerprint(request: Request) -> str:
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
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def idempotent(required: bool = True) -> Callable[[Callable[..., Response]], Callable[..., Response]]:
    def decorate(view: Callable[..., Response]) -> Callable[..., Response]:
        @wraps(view)
        def wrapped(self: Any, request: Request, *args: Any, **kwargs: Any) -> Response:
            if not request.user.is_authenticated or request.user.pk is None:
                raise ImproperlyConfigured("Idempotency requires an authenticated actor")
            try:
                raw_key = request.headers.get("Idempotency-Key", "")
                key = uuid.UUID(raw_key)
                if len(raw_key) != 36 or raw_key.lower() != str(key):
                    raise ValueError("Idempotency-Key must be a standard 36-character UUID")
            except ValueError as exc:
                if required:
                    raise IdempotencyKeyRequired() from exc
                return view(self, request, *args, **kwargs)
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
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", [lock_scope])
                    acquired = cursor.fetchone()
                if not acquired or not acquired[0]:
                    raise RequestInProgress()
                fingerprint = request_fingerprint(request)
                record = IdempotencyRecord.objects.filter(**scope).first()
                if record is not None and record.expires_at <= timeutils.now():
                    record.delete()
                    record = None
                if record is not None:
                    if record.request_hash != fingerprint:
                        raise IdempotencyKeyReused()
                    return Response(
                        record.response_body,
                        status=record.response_status,
                        headers={"Idempotent-Replayed": "true"},
                    )
                response = view(self, request, *args, **kwargs)
                if 200 <= response.status_code < 300:
                    IdempotencyRecord.objects.create(
                        **scope,
                        request_hash=fingerprint,
                        response_status=response.status_code,
                        response_body=response.data,
                    )
                else:
                    transaction.set_rollback(True)
                return response

        return wrapped

    return decorate
