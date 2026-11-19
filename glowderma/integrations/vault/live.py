"""KV v2 over HTTP to a local Vault Agent ``api_proxy`` (``VAULT_MODE=live``).

The agent adds its own auto-auth token (``use_auto_auth_token = "force"``), so this adapter sends **no
token** and holds none. ``POST /v1/<mount>/data/<path>`` writes a new version (``{"data": {...}}`` →
``data.version``); ``GET /v1/<mount>/data/<path>?version=N`` reads one (``data.data``). Calls go through
``integrations.httpclient`` (bounded, retried only when nothing left the process, paths-only logs); a 404 is
:class:`VaultNotFound`, every other non-2xx or transport failure :class:`VaultUnavailable`. Bodies are never
logged and errors never carry a value.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings

from glowderma.integrations import httpclient
from glowderma.integrations.secret import Secret

from .ports import VaultNotConfigured, VaultNotFound, VaultUnavailable

logger = logging.getLogger(__name__)

PROVIDER = "vault"
TIMEOUT_SECONDS = 5.0


class LiveVault:
    name = "live"

    def __init__(
        self, proxy_url: str, mount: str = "secret", *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.proxy_url = proxy_url.rstrip("/")
        self.mount = mount.strip("/")
        self._transport = transport

    @classmethod
    def from_settings(cls) -> LiveVault:
        url = str(getattr(settings, "VAULT_PROXY_URL", "") or "")
        if not url:
            raise VaultNotConfigured("VAULT_PROXY_URL is not set")
        return cls(url, str(getattr(settings, "VAULT_KV_MOUNT", "secret") or "secret"))

    def is_configured(self) -> bool:
        return bool(self.proxy_url and self.mount)

    def _url(self, path: str) -> str:
        clean = "/".join(quote(part, safe="") for part in path.strip("/").split("/") if part)
        if not clean:
            raise ValueError("empty Vault path")
        return f"{self.proxy_url}/v1/{quote(self.mount, safe='')}/data/{clean}"

    def _send(self, method: str, url: str, *, retry_unsent: bool = True, **kwargs: Any) -> httpclient.Answer:
        with httpclient.new_client(self._transport) as http:
            try:
                return httpclient.send(
                    http,
                    method,
                    url,
                    provider=PROVIDER,
                    deadline=httpclient.Deadline(TIMEOUT_SECONDS),
                    retry_unsent=retry_unsent,
                    **kwargs,
                )
            except httpclient.TransportFailure as exc:
                raise VaultUnavailable(
                    f"vault {method} {httpclient.path_of(url)}: {exc}", sent=exc.sent
                ) from None

    def write(self, path: str, values: Mapping[str, str | Secret]) -> int:
        url = self._url(path)
        body = {"data": {key: v.reveal() if isinstance(v, Secret) else str(v) for key, v in values.items()}}
        answer = self._send("POST", url, json=body)
        if not answer.ok:
            raise VaultUnavailable(f"vault write {httpclient.path_of(url)} -> {answer.status}", sent=True)
        version = httpclient.int_code((answer.json_object().get("data") or {}).get("version"))
        if version is None or version < 1:
            raise VaultUnavailable(
                f"vault write {httpclient.path_of(url)}: no version in the answer", sent=True
            )
        return version

    def read(self, path: str, version: int | None = None) -> dict[str, Secret]:
        url = self._url(path)
        params = {"version": str(version)} if version is not None else None
        answer = self._send("GET", url, params=params)
        if answer.status == 404:
            raise VaultNotFound(f"vault read {httpclient.path_of(url)} version {version}: not found")
        if not answer.ok:
            raise VaultUnavailable(f"vault read {httpclient.path_of(url)} -> {answer.status}")
        data = answer.json_object().get("data") or {}
        values = data.get("data") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            # KV v2 answers 200 with data: null for a deleted (soft) version
            raise VaultNotFound(f"vault read {httpclient.path_of(url)} version {version}: deleted")
        return {str(key): Secret(str(value)) for key, value in values.items() if isinstance(value, str)}
