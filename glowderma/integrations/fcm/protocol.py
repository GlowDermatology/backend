"""FCM HTTP v1 protocol (customer contract §2.14.2) — pure helpers: the message document and the answer
classification. Sources: firebase.google.com/docs/reference/fcm/rest/v1/projects.messages (message
fields), firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode (error codes),
developers.google.com/identity/protocols/oauth2/service-account (JWT-bearer grant). Written from those
references; not verified against a live project (verify M3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .ports import PushMessage

SEND_URL: Final = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
SCOPE: Final = "https://www.googleapis.com/auth/firebase.messaging"
JWT_BEARER_GRANT: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_LIFETIME_SECONDS: Final = 3600  # Google's maximum
FCM_ERROR_TYPE: Final = "type.googleapis.com/google.firebase.fcm.v1.FcmError"
BAD_REQUEST_TYPE: Final = "type.googleapis.com/google.rpc.BadRequest"
TOKEN_FIELD: Final = "message.token"  # noqa: S105 - a field name
TOKEN_PHRASE: Final = "registration token"  # noqa: S105 - error text

# Classification kinds (see :func:`classify`).
OK: Final = "ok"
DEAD_TOKEN: Final = "dead_token"  # noqa: S105 - a classification kind
PAYLOAD: Final = "payload"
THIRD_PARTY_AUTH: Final = "third_party_auth"
TRANSIENT: Final = "transient"
AUTH: Final = "auth"
CONFIG: Final = "config"
TRANSIENT_CODES: Final = frozenset({"UNAVAILABLE", "INTERNAL", "QUOTA_EXCEEDED"})
DEAD_CODES: Final = frozenset({"UNREGISTERED", "SENDER_ID_MISMATCH"})


def message_document(message: PushMessage) -> dict[str, Any]:
    """The ``messages:send`` body of contract §2.14.2: notification block (omitted for data-only), string
    ``data``, Android priority/channel and APNs headers/payload (data-only = background push, priority 5)."""
    android: dict[str, Any] = {"priority": message.android_priority}
    headers = {"apns-priority": str(message.apns_priority), "apns-push-type": message.apns_push_type}
    if message.collapse_key:
        android["collapse_key"] = message.collapse_key
        headers["apns-collapse-id"] = message.collapse_key
    document: dict[str, Any] = {"token": message.token, "data": {k: str(v) for k, v in message.data.items()}}
    if message.data_only:
        aps: dict[str, Any] = {"content-available": 1}
    else:
        notification = {"title": message.title, "body": message.body}
        if message.image_url:
            notification["image"] = message.image_url
        document["notification"] = notification
        if message.android_channel:
            android["notification"] = {"channel_id": message.android_channel}
        aps = {"sound": "default", "mutable-content": 1}
    document["android"] = android
    document["apns"] = {"headers": headers, "payload": {"aps": aps}}
    return {"message": document}


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: str
    message_id: str = ""
    code: str = ""  # FCM's errorCode or the HTTP status, for logs (never the token)


def _details(error: dict[str, Any]) -> tuple[str, list[str]]:
    code = ""
    fields: list[str] = []
    details = error.get("details")
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get("@type") == FCM_ERROR_TYPE and isinstance(detail.get("errorCode"), str):
            code = detail["errorCode"]
        if detail.get("@type") == BAD_REQUEST_TYPE:
            violations = detail.get("fieldViolations")
            for violation in violations if isinstance(violations, list) else []:
                if isinstance(violation, dict) and isinstance(violation.get("field"), str):
                    fields.append(violation["field"])
    return code, fields


def classify(status: int, body: object) -> Verdict:
    """What one ``messages:send`` answer means for the device and the batch.

    * 200 → ``ok`` with the message id (the last segment of ``name``);
    * ``UNREGISTERED`` / ``SENDER_ID_MISMATCH``, or ``INVALID_ARGUMENT`` naming the registration token
      (field ``message.token`` or the "registration token" message) → ``dead_token`` (prune the device);
    * any other ``INVALID_ARGUMENT`` / 400 / unexpected 4xx → ``payload`` (the message failed, device
      untouched);
    * ``THIRD_PARTY_AUTH_ERROR`` (APNs credentials of the Firebase project) → ``third_party_auth``;
    * ``UNAVAILABLE`` / ``INTERNAL`` / ``QUOTA_EXCEEDED`` / 429 / 5xx → ``transient`` (retry the batch);
    * 401 without an FCM code → ``auth`` (our access token was refused: refresh once);
    * 403 / 404 without a token code → ``config`` (wrong project or a service account without permission).
    """
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    if status == 200:
        name = body.get("name") if isinstance(body, dict) else None
        message_id = name.rsplit("/", 1)[-1] if isinstance(name, str) else ""
        return Verdict(OK, message_id=message_id)
    code, fields = _details(error)
    status_name = error.get("status") if isinstance(error.get("status"), str) else ""
    text = error.get("message") if isinstance(error.get("message"), str) else ""
    label = code or status_name or f"HTTP {status}"
    if code in DEAD_CODES:
        return Verdict(DEAD_TOKEN, code=label)
    if code == "THIRD_PARTY_AUTH_ERROR":
        return Verdict(THIRD_PARTY_AUTH, code=label)
    if code in TRANSIENT_CODES or status == 429 or status >= 500:
        return Verdict(TRANSIENT, code=label)
    if code == "INVALID_ARGUMENT" or status_name == "INVALID_ARGUMENT" or status == 400:
        names_token = TOKEN_FIELD in fields or TOKEN_PHRASE in str(text).lower()
        return Verdict(DEAD_TOKEN if names_token else PAYLOAD, code=label)
    if status == 401:
        return Verdict(AUTH, code=label)
    if status in (403, 404):
        return Verdict(CONFIG, code=label)
    return Verdict(PAYLOAD, code=label)
