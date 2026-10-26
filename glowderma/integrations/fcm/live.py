"""Live FCM adapter (``FCM_MODE=live``): HTTP v1 over httpx with a service-account OAuth2 token minted by a
PyJWT RS256 JWT-bearer grant — no firebase SDK (customer contract §2.14.2, ``docs/integrations/fcm.md``).

* **Credentials:** ``FCM_CREDENTIALS_FILE`` (the service-account JSON key) is read when a token is minted;
  its contents are never logged or put into an exception message. ``FCM_PROJECT_ID`` names the project.
* **Access token:** ``POST https://oauth2.googleapis.com/token`` (``grant_type`` JWT bearer, assertion ``iss``
  = the service account, ``scope`` firebase.messaging, ``aud`` the token endpoint, one hour). The token is
  cached **in the process** (never in Redis or the database) until five minutes before it expires, minted
  single-flight under a lock, and refreshed once when FCM answers 401 for it.
* **Send:** one ``POST …/messages:send`` per message, at most :data:`MAX_CONCURRENCY` in flight; each call is
  bounded (3 s connect, 10 s in total). Results come back in order, one :class:`PushResult` per message.

Outcomes per message (:func:`.protocol.classify`): accepted → ``PushResult(ok, message_id)``; a dead token
(``UNREGISTERED``, ``SENDER_ID_MISMATCH``, ``INVALID_ARGUMENT`` naming the token) → ``unregistered`` (the
caller prunes the device); any other ``INVALID_ARGUMENT`` → ``payload_invalid`` (device untouched);
``THIRD_PARTY_AUTH_ERROR`` → ``third_party_auth_error`` (ERROR log; the device is fine, the project's APNs
credentials are not). Transport failures, 5xx, ``UNAVAILABLE`` / ``INTERNAL`` / ``QUOTA_EXCEEDED``, a refused
access token and a project/permission error stop the batch: when **no** message of the batch was accepted the
whole call raises :class:`FcmUnavailable` (the caller gives every claim back and the outbox retries); once a
message was accepted the batch cannot be retried without sending it twice, so the messages that did not go out
answer ``unavailable`` (failed, device untouched).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings

from .. import httpclient
from . import protocol
from .ports import PAYLOAD_INVALID, UNREGISTERED, FcmUnavailable, PushMessage, PushResult

logger = logging.getLogger(__name__)
PROVIDER = "fcm"
MAX_CONCURRENCY = 8
REFRESH_MARGIN_SECONDS = 300
UNAVAILABLE = "unavailable"
THIRD_PARTY_AUTH_ERROR = "third_party_auth_error"
STOPPING = frozenset({protocol.TRANSIENT, protocol.AUTH, protocol.CONFIG})


class ServiceAccountError(ValueError):
    """The service-account file is missing or malformed (the message names the problem, never the
    contents)."""


@dataclass(frozen=True, slots=True)
class ServiceAccount:
    client_email: str
    private_key: str = ""
    private_key_id: str = ""

    def __repr__(self) -> str:  # never print the key
        return f"ServiceAccount(client_email={self.client_email!r})"


def load_service_account(path: str) -> ServiceAccount:
    """Read and check a Google service-account JSON key file (type, client email, an RSA private key)."""
    if not path:
        raise ServiceAccountError("FCM_CREDENTIALS_FILE is not set")
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ServiceAccountError(f"FCM_CREDENTIALS_FILE is not readable ({type(exc).__name__})") from None
    except ValueError:
        raise ServiceAccountError("FCM_CREDENTIALS_FILE is not JSON") from None
    if not isinstance(raw, dict) or raw.get("type") != "service_account":
        raise ServiceAccountError("FCM_CREDENTIALS_FILE is not a service-account key")
    email, key, key_id = raw.get("client_email"), raw.get("private_key"), raw.get("private_key_id")
    if not isinstance(email, str) or "@" not in email:
        raise ServiceAccountError("FCM_CREDENTIALS_FILE has no client_email")
    if not isinstance(key, str):
        raise ServiceAccountError("FCM_CREDENTIALS_FILE has no private_key")
    try:
        loaded = serialization.load_pem_private_key(key.encode(), password=None)
    except ValueError, TypeError:
        raise ServiceAccountError("FCM_CREDENTIALS_FILE private_key is not a PEM private key") from None
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise ServiceAccountError("FCM_CREDENTIALS_FILE private_key is not an RSA key")
    return ServiceAccount(email, key, key_id if isinstance(key_id, str) else "")


class AccessTokens:
    """Process-wide OAuth2 access tokens per service account, minted single-flight under one lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, key: str, mint: Callable[[], tuple[str, int]]) -> str:
        with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached[1] > time.monotonic():
                return cached[0]
            token, expires_in = mint()
            lifetime = max(60, expires_in - REFRESH_MARGIN_SECONDS)
            self._tokens[key] = (token, time.monotonic() + lifetime)
            return token

    def invalidate(self, key: str, stale: str) -> None:
        with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached[0] == stale:
                del self._tokens[key]

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()


access_tokens = AccessTokens()


@dataclass(frozen=True, slots=True)
class _Outcome:
    kind: str
    message_id: str = ""
    code: str = ""


SKIPPED = _Outcome(protocol.TRANSIENT, code="not sent: the batch stopped")


class LiveFcm:
    name = "fcm"

    def __init__(
        self,
        *,
        project_id: str,
        credentials_file: str,
        transport: httpx.BaseTransport | None = None,
        concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        self.project_id = project_id
        self.credentials_file = credentials_file
        self.transport = transport
        self.concurrency = max(1, concurrency)

    @classmethod
    def from_settings(cls) -> LiveFcm:
        return cls(
            project_id=str(settings.FCM_PROJECT_ID), credentials_file=str(settings.FCM_CREDENTIALS_FILE)
        )

    @property
    def _token_key(self) -> str:
        return f"{self.project_id}:{self.credentials_file}"

    # --- access token ---------------------------------------------------------------------------------------

    def _mint(self, http: httpx.Client, deadline: httpclient.Deadline) -> tuple[str, int]:
        try:
            account = load_service_account(self.credentials_file)
        except ServiceAccountError as exc:
            logger.error("fcm.credentials_invalid %s", exc)
            raise FcmUnavailable(str(exc)) from None
        issued = int(time.time())
        claims = {
            "iss": account.client_email,
            "scope": protocol.SCOPE,
            "aud": protocol.TOKEN_URL,
            "iat": issued,
            "exp": issued + protocol.ASSERTION_LIFETIME_SECONDS,
        }
        headers = {"kid": account.private_key_id} if account.private_key_id else None
        assertion = jwt.encode(claims, account.private_key, algorithm="RS256", headers=headers)
        try:
            answer = httpclient.send(
                http,
                "POST",
                protocol.TOKEN_URL,
                provider=PROVIDER,
                deadline=deadline,
                data={"grant_type": protocol.JWT_BEARER_GRANT, "assertion": assertion},
                headers={"Accept": "application/json"},
            )
        except httpclient.TransportFailure as exc:
            raise FcmUnavailable(f"Google token endpoint: {exc}") from exc
        body = answer.json_object()
        token, expires_in = body.get("access_token"), body.get("expires_in")
        if answer.status == 200 and isinstance(token, str) and token:
            return token, expires_in if isinstance(expires_in, int) and expires_in > 0 else 3600
        if 400 <= answer.status < 500:
            error = httpclient.safe_text(body.get("error"))
            logger.error("fcm.auth_failed token endpoint HTTP %s %s", answer.status, error)
        raise FcmUnavailable(f"Google token endpoint: HTTP {answer.status}")

    # --- one message ----------------------------------------------------------------------------------------

    def _send_one(self, http: httpx.Client, message: PushMessage) -> _Outcome:
        deadline = httpclient.Deadline()
        url = protocol.SEND_URL.format(project_id=self.project_id)
        document = protocol.message_document(message)
        for attempt in (1, 2):
            try:
                token = access_tokens.get(self._token_key, lambda: self._mint(http, deadline))
            except FcmUnavailable as exc:
                return _Outcome(protocol.CONFIG, code=str(exc))
            try:
                answer = httpclient.send(
                    http,
                    "POST",
                    url,
                    provider=PROVIDER,
                    deadline=deadline,
                    json=document,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpclient.TransportFailure as exc:
                return _Outcome(protocol.TRANSIENT, code=str(exc))
            verdict = protocol.classify(answer.status, answer.data)
            if verdict.kind == protocol.AUTH and attempt == 1:
                access_tokens.invalidate(self._token_key, token)
                continue
            return _Outcome(verdict.kind, verdict.message_id, verdict.code)
        raise AssertionError("unreachable")  # pragma: no cover - the second attempt always returns

    # --- port -----------------------------------------------------------------------------------------------

    def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        batch = list(messages)
        if not batch:
            return []
        stop = threading.Event()

        def run(http: httpx.Client, message: PushMessage) -> _Outcome:
            if stop.is_set():
                return SKIPPED
            outcome = self._send_one(http, message)
            if outcome.kind in STOPPING:
                stop.set()
            return outcome

        with httpclient.new_client(self.transport) as http:
            if len(batch) == 1:
                outcomes = [run(http, batch[0])]
            else:
                workers = min(self.concurrency, len(batch))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fcm-send") as pool:
                    outcomes = list(pool.map(lambda message: run(http, message), batch))
        return self._results(outcomes)

    def _results(self, outcomes: list[_Outcome]) -> list[PushResult]:
        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
        stopped = [outcome for outcome in outcomes if outcome.kind in STOPPING]
        for outcome in stopped:
            if outcome.kind in (protocol.AUTH, protocol.CONFIG):
                logger.error(
                    "fcm.config_error %s (check FCM_PROJECT_ID and the service account)", outcome.code
                )
                break
        if counts.get(protocol.THIRD_PARTY_AUTH):
            logger.error(
                "fcm.third_party_auth_error count=%s (APNs credentials of the Firebase project)",
                counts[protocol.THIRD_PARTY_AUTH],
            )
        logger.info("fcm.send %s", {kind: counts[kind] for kind in sorted(counts)})
        if stopped and not counts.get(protocol.OK):
            raise FcmUnavailable(f"FCM did not accept the batch: {stopped[0].code}")
        results: list[PushResult] = []
        for outcome in outcomes:
            if outcome.kind == protocol.OK:
                results.append(PushResult(ok=True, message_id=outcome.message_id[:128]))
            elif outcome.kind == protocol.DEAD_TOKEN:
                results.append(PushResult(ok=False, error=UNREGISTERED))
            elif outcome.kind == protocol.PAYLOAD:
                results.append(PushResult(ok=False, error=PAYLOAD_INVALID))
            elif outcome.kind == protocol.THIRD_PARTY_AUTH:
                results.append(PushResult(ok=False, error=THIRD_PARTY_AUTH_ERROR))
            else:
                results.append(PushResult(ok=False, error=UNAVAILABLE))
        return results
