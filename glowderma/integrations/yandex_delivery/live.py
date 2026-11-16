"""Live Yandex Delivery adapter (``YANDEX_DELIVERY_MODE=live``; endpoints and parsers in :mod:`.protocol`).

Every request carries ``Authorization: Bearer <YANDEX_DELIVERY_TOKEN>`` (the OAuth token of the business
cabinet; it never appears in a URL, a log line or an exception) and ``Accept-Language``
(``YANDEX_DELIVERY_LANGUAGE``). Each port call shares one :class:`~glowderma.integrations.httpclient.Deadline`
— 10 s, or the one the caller passes (a customer request shares ``CUSTOMER_PATH_SECONDS``); ``bulk_info``
chunks each get their own unless the caller passed one.

Failure mapping (AGENTS.md "Live adapters"): no answer, or a request that may have left without an answer
(timeout, broken body), and HTTP 5xx → :class:`YandexUnavailable` with ``outcome_unknown`` — never resent
here (``claims/create`` is repeated by the caller with the same ``request_id``, which the documentation
prescribes for exactly that case); a redirect (never followed), 401/403/408/425/429 →
:class:`YandexUnavailable` "not processed" (429 is Yandex's rate limit, not a refusal; a 401 also logs ERROR
``yandex.auth_failed``); a 2xx we cannot read, or a price in another currency → :class:`YandexUnavailable`;
409 of ``accept`` / ``cancel`` / ``return`` / ``tracking-links`` → :class:`YandexConflict`; any other 4xx
→ :class:`YandexRejected` with Yandex's ``code``. Yandex's ``message`` goes through ``httpclient.safe_text``.

Logs carry the path, the status and the duration only (``httpclient``), plus the ERROR keys
``yandex.auth_failed`` and ``yandex.currency_mismatch`` and the WARNING ``yandex.unreadable_claim`` — no
token, name, phone or address.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx
from django.conf import settings

from .. import httpclient
from . import protocol
from .ports import (
    CancelState,
    CancelTerms,
    Claim,
    ClaimRequest,
    Estimate,
    ParcelItem,
    Point,
    Tariff,
    YandexConflict,
    YandexError,
    YandexRejected,
    YandexUnavailable,
)

logger = logging.getLogger(__name__)
PROVIDER = "yandex"
# What reading an unexpected answer may raise; mapped to YandexUnavailable, never an untyped exception.
_SHAPE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, OverflowError, IndexError, ArithmeticError)


class LiveYandexDelivery:
    name = "yandex"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        currency: str = "UZS",
        language: str = "ru",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.currency = currency
        self.language = language
        self.transport = transport

    def __repr__(self) -> str:  # never the token
        return f"LiveYandexDelivery(base_url={self.base_url!r})"

    @classmethod
    def from_settings(cls) -> LiveYandexDelivery:
        return cls(
            base_url=str(settings.YANDEX_DELIVERY_BASE_URL),
            token=str(settings.YANDEX_DELIVERY_TOKEN),
            currency=str(settings.YANDEX_DELIVERY_CURRENCY),
            language=str(settings.YANDEX_DELIVERY_LANGUAGE),
        )

    def is_configured(self) -> bool:
        """The readiness probe (``health/?ready=1``; settings only, nothing is sent): a token, an https base
        URL, a three-letter currency and a documented language."""
        return (
            bool(self._token.strip())
            and self.base_url.startswith("https://")
            and bool(re.fullmatch(r"[A-Z]{3}", self.currency))
            and self.language in protocol.LANGUAGES
        )

    # --- transport ------------------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept-Language": self.language,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _call(
        self,
        method: str,
        path: str,
        *,
        deadline: httpclient.Deadline | None,
        conflict: bool = False,
        **kwargs: Any,
    ) -> httpclient.Answer:
        """One request; returns a 2xx answer, raises the typed error of anything else."""
        try:
            with httpclient.new_client(self.transport) as http:
                answer = httpclient.send(
                    http,
                    method,
                    self.base_url + path,
                    provider=PROVIDER,
                    deadline=deadline or httpclient.Deadline(),
                    headers=self._headers(),
                    **kwargs,
                )
        except httpclient.TransportFailure as exc:
            raise YandexUnavailable(f"Yandex {path}: {exc}", outcome_unknown=exc.sent) from exc
        if answer.status == 401:
            logger.error("yandex.auth_failed status=401 (check YANDEX_DELIVERY_TOKEN)")
        if answer.not_processed:
            raise YandexUnavailable(
                f"Yandex {path}: HTTP {answer.status} (not processed)",
                outcome_unknown=False,
                status=answer.status,
            )
        if answer.status >= 500 or answer.status < 200:
            raise YandexUnavailable(f"Yandex {path}: HTTP {answer.status}", status=answer.status)
        if answer.ok:
            return answer
        code = protocol.error_code(answer.data, answer.status)
        message = httpclient.safe_text(protocol.error_message(answer.data))
        if answer.status == 409 and conflict:
            raise YandexConflict(code, message)
        raise YandexRejected(code, message)

    def _read[T](self, path: str, read: Callable[[], T]) -> T:
        """``read()``, with an answer of an unexpected shape (or another currency) as YandexUnavailable."""
        try:
            return read()
        except YandexError:
            raise
        except protocol.CurrencyMismatch as exc:
            logger.error("yandex.currency_mismatch path=%s expected=%s", path, self.currency)
            raise YandexUnavailable(f"Yandex {path}: {exc}") from exc
        except _SHAPE_ERRORS as exc:
            raise YandexUnavailable(f"Yandex {path}: unreadable answer ({type(exc).__name__})") from exc

    @staticmethod
    def _object(answer: httpclient.Answer, path: str) -> dict[str, Any]:
        if not isinstance(answer.data, dict):
            raise YandexUnavailable(f"Yandex {path}: answer is not a JSON object")
        return answer.data

    @staticmethod
    def _claim_param(claim_id: str) -> dict[str, str]:
        if not protocol.valid_claim_id(claim_id):
            raise YandexRejected("invalid_claim_id", "not a Yandex claim id")
        return {"claim_id": claim_id}

    @staticmethod
    def _payload[T](build: Callable[[], T]) -> T:
        """Our own data that cannot be sent (a phone that is not +998…, coordinates out of range) is a
        definitive refusal before any request."""
        try:
            return build()
        except ValueError as exc:
            raise YandexRejected("invalid_request", str(exc)) from exc

    # --- port -----------------------------------------------------------------------------------------------

    def tariffs(self, point: Point, *, deadline: httpclient.Deadline | None = None) -> list[Tariff]:
        body = self._payload(lambda: protocol.tariffs_payload(point))
        answer = self._call("POST", protocol.PATH_TARIFFS, deadline=deadline, json=body)
        data = self._object(answer, protocol.PATH_TARIFFS)
        return self._read(protocol.PATH_TARIFFS, lambda: protocol.tariffs_from(data))

    def check_price(
        self,
        source: Point,
        destination: Point,
        item: ParcelItem,
        taxi_class: str,
        *,
        deadline: httpclient.Deadline | None = None,
    ) -> Estimate:
        body = self._payload(lambda: protocol.check_price_payload(source, destination, item, taxi_class))
        answer = self._call("POST", protocol.PATH_CHECK_PRICE, deadline=deadline, json=body)
        data = self._object(answer, protocol.PATH_CHECK_PRICE)
        return self._read(
            protocol.PATH_CHECK_PRICE, lambda: protocol.estimate_from(data, currency=self.currency)
        )

    def create_claim(self, request: ClaimRequest, *, deadline: httpclient.Deadline | None = None) -> Claim:
        body = self._payload(lambda: protocol.create_payload(request, currency=self.currency))
        answer = self._call(
            "POST",
            protocol.PATH_CREATE,
            deadline=deadline,
            params={"request_id": request.request_id},
            json=body,
        )
        # A 2xx we cannot read may still be a created claim: the retry with the same request_id returns it.
        data = self._object(answer, protocol.PATH_CREATE)
        return self._read(protocol.PATH_CREATE, lambda: protocol.claim_from(data, currency=self.currency))

    def claim(self, claim_id: str, *, deadline: httpclient.Deadline | None = None) -> Claim:
        answer = self._call("POST", protocol.PATH_INFO, deadline=deadline, params=self._claim_param(claim_id))
        data = self._object(answer, protocol.PATH_INFO)
        return self._read(protocol.PATH_INFO, lambda: protocol.claim_from(data, currency=self.currency))

    def claims(self, claim_ids: list[str], *, deadline: httpclient.Deadline | None = None) -> list[Claim]:
        ids = list(dict.fromkeys(claim_id for claim_id in claim_ids if protocol.valid_claim_id(claim_id)))
        found: list[Claim] = []
        for chunk in protocol.chunks(ids):
            answer = self._call("POST", protocol.PATH_BULK_INFO, deadline=deadline, json={"claim_ids": chunk})
            data = self._object(answer, protocol.PATH_BULK_INFO)
            entries = self._read(protocol.PATH_BULK_INFO, partial(protocol.bulk_entries, data))
            for entry in entries:
                snapshot = self._bulk_claim(entry)
                if snapshot is not None:
                    found.append(snapshot)
        return found

    def _bulk_claim(self, entry: dict[str, Any]) -> Claim | None:
        """One ``bulk_info`` entry; an unreadable one is skipped (the others still sync) and logged."""
        try:
            return protocol.claim_from(entry, currency=self.currency)
        except protocol.CurrencyMismatch:
            logger.error(
                "yandex.currency_mismatch path=%s expected=%s", protocol.PATH_BULK_INFO, self.currency
            )
        except _SHAPE_ERRORS as exc:
            claim_id = entry.get("id")
            logger.warning(
                "yandex.unreadable_claim claim_id=%s error=%s",
                claim_id if isinstance(claim_id, str) and protocol.valid_claim_id(claim_id) else "?",
                type(exc).__name__,
            )
        return None

    def accept(
        self, claim_id: str, version: int, *, deadline: httpclient.Deadline | None = None
    ) -> Claim | None:
        answer = self._call(
            "POST",
            protocol.PATH_ACCEPT,
            deadline=deadline,
            conflict=True,
            params=self._claim_param(claim_id),
            json={"version": version},
        )
        data = answer.json_object()
        try:  # the accept was processed: an unreadable body only means "re-read the claim"
            accepted_id, status, accepted_version = protocol.claim_identity(data)
        except _SHAPE_ERRORS:
            return None
        return Claim(claim_id=accepted_id, status=status, version=accepted_version)

    def cancel_terms(self, claim_id: str, *, deadline: httpclient.Deadline | None = None) -> CancelTerms:
        answer = self._call(
            "POST", protocol.PATH_CANCEL_INFO, deadline=deadline, params=self._claim_param(claim_id)
        )
        data = self._object(answer, protocol.PATH_CANCEL_INFO)
        return self._read(
            protocol.PATH_CANCEL_INFO, lambda: protocol.cancel_terms_from(data, currency=self.currency)
        )

    def cancel(
        self, claim_id: str, version: int, state: CancelState, *, deadline: httpclient.Deadline | None = None
    ) -> None:
        body = protocol.cancel_payload(version, state)
        self._call(
            "POST",
            protocol.PATH_CANCEL,
            deadline=deadline,
            conflict=True,
            params=self._claim_param(claim_id),
            json=body,
        )

    def request_return(
        self, claim_id: str, point_id: int, comment: str, *, deadline: httpclient.Deadline | None = None
    ) -> None:
        self._call(
            "POST",
            protocol.PATH_RETURN,
            deadline=deadline,
            conflict=True,
            params=self._claim_param(claim_id),
            json=protocol.return_payload(point_id, comment),
        )

    def tracking_link(self, claim_id: str, *, deadline: httpclient.Deadline | None = None) -> str | None:
        try:
            answer = self._call(
                "GET",
                protocol.PATH_TRACKING_LINKS,
                deadline=deadline,
                conflict=True,
                params=self._claim_param(claim_id),
            )
        except YandexConflict:  # "inactive claim or no courier tracking links" (yet)
            return None
        return protocol.sharing_link_from(answer.json_object())
