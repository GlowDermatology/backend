"""The Vault KV v2 port: the in-memory fake, the live adapter against a scripted agent proxy, the registry."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured

from config.secrets import parse_secrets
from glowderma.integrations.secret import Secret
from glowderma.integrations.tests.live_fixtures import Script, connect_error, read_timeout
from glowderma.integrations.vault.fake import FakeVault
from glowderma.integrations.vault.live import LiveVault
from glowderma.integrations.vault.ports import VaultNotConfigured, VaultNotFound, VaultUnavailable
from glowderma.integrations.vault.registry import fake_vault, get_vault

PATH = "production/ai/gemini"
DATA_PATH = f"/v1/secret/data/{PATH}"
KEY = "AIza-vault-test-0123456789"


@pytest.fixture
def fake() -> Iterator[FakeVault]:
    vault = fake_vault()
    vault.reset()
    yield vault
    vault.reset()


def test_fake_keeps_versions(fake: FakeVault) -> None:
    assert fake.write(PATH, {"api_key": KEY}) == 1
    assert fake.write(PATH, {"api_key": Secret("second")}) == 2
    assert fake.read(PATH, 1) == {"api_key": Secret(KEY)}
    assert fake.read(PATH) == {"api_key": Secret("second")}
    assert fake.current_version(PATH) == 2
    fake.destroy(PATH, 1)
    for version in (1, 3, 0):
        with pytest.raises(VaultNotFound):
            fake.read(PATH, version)
    with pytest.raises(VaultNotFound):
        fake.read("other/path")
    fake.fail_next(VaultUnavailable("down"))
    with pytest.raises(VaultUnavailable):
        fake.write(PATH, {"api_key": "x"})
    assert fake.current_version(PATH) == 2 and fake.writes == 2 and fake.is_configured()


def test_registry(settings: Any, fake: FakeVault) -> None:
    settings.VAULT_MODE = "fake"
    assert get_vault() is fake
    settings.VAULT_MODE = "live"
    settings.VAULT_PROXY_URL = "http://vault-agent-ai:8100"
    live = get_vault()
    assert isinstance(live, LiveVault) and live.is_configured()
    settings.VAULT_PROXY_URL = ""
    with pytest.raises(VaultNotConfigured):
        get_vault()
    settings.VAULT_MODE = "nope"
    with pytest.raises(ImproperlyConfigured):
        get_vault()


def test_registry_refuses_the_fake_under_production_settings(settings: Any) -> None:
    settings.VAULT_MODE = "fake"
    settings.DEV_FEATURES_ALLOWED = False
    with pytest.raises(VaultNotConfigured):
        get_vault()


def live(script: Script) -> LiveVault:
    return LiveVault("http://vault-agent-ai:8100/", "secret", transport=script.transport)


def test_live_write_posts_data_without_a_token_and_returns_the_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = Script().on("POST", DATA_PATH, httpx.Response(200, json={"data": {"version": 4}}))
    with caplog.at_level(logging.DEBUG):
        assert live(script).write(PATH, {"api_key": Secret(KEY)}) == 4
    [sent] = script.requests
    assert json.loads(sent.content) == {"data": {"api_key": KEY}}
    assert "x-vault-token" not in sent.headers and "authorization" not in sent.headers
    assert KEY not in caplog.text


def test_live_read_returns_secrets_for_a_version() -> None:
    body = {"data": {"data": {"api_key": KEY, "n": 1}, "metadata": {"version": 3}}}
    script = Script().on("GET", DATA_PATH, httpx.Response(200, json=body))
    values = live(script).read(PATH, 3)
    assert values == {"api_key": Secret(KEY)}
    assert script.requests[0].url.params["version"] == "3"
    live(script).read(PATH)
    assert "version" not in script.requests[1].url.params


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404, json={"errors": []}), VaultNotFound),
        (
            httpx.Response(200, json={"data": {"data": None, "metadata": {"deletion_time": "x"}}}),
            VaultNotFound,
        ),
        (httpx.Response(403, json={"errors": ["permission denied"]}), VaultUnavailable),
        (httpx.Response(503, json={"errors": ["sealed"]}), VaultUnavailable),
        (connect_error, VaultUnavailable),
        (read_timeout, VaultUnavailable),
    ],
)
def test_live_read_failures(response: Any, error: type[Exception]) -> None:
    script = Script().on("GET", DATA_PATH, response)
    with pytest.raises(error) as caught:
        live(script).read(PATH, 2)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("response", "sent"),
    [
        (httpx.Response(403, json={"errors": ["permission denied"]}), True),
        (httpx.Response(200, json={"data": {}}), True),
        (connect_error, False),
        (read_timeout, True),
    ],
)
def test_live_write_failures(response: Any, sent: bool) -> None:
    script = Script().on("POST", DATA_PATH, response)
    with pytest.raises(VaultUnavailable) as caught:
        live(script).write(PATH, {"api_key": KEY})
    assert caught.value.sent is sent
    assert KEY not in str(caught.value)


def test_live_quotes_path_segments_and_refuses_empty_paths() -> None:
    script = Script().on("GET", "/v1/secret/data/a b/c", httpx.Response(200, json={"data": {"data": {}}}))
    assert live(script).read("/a b/c/") == {}
    assert script.requests[0].url.raw_path.startswith(b"/v1/secret/data/a%20b/c")
    with pytest.raises(ValueError):
        live(script).read("//")


def test_live_from_settings(settings: Any) -> None:
    settings.VAULT_PROXY_URL = "http://vault-agent-ai-reader:8100"
    settings.VAULT_KV_MOUNT = "kv"
    adapter = LiveVault.from_settings()
    assert adapter.proxy_url == "http://vault-agent-ai-reader:8100" and adapter.mount == "kv"


@pytest.mark.parametrize("key", ["VAULT_MODE", "VAULT_PROXY_URL", "VAULT_KV_MOUNT", "VAULT_AI_PREFIX"])
def test_the_secrets_file_cannot_set_the_vault_topology(key: str) -> None:
    # Per service (api → writer agent, worker → reader agent): a shared copy would give the worker the writer.
    with pytest.raises(ImproperlyConfigured, match=key):
        parse_secrets(json.dumps({key: "x"}), source="f")
