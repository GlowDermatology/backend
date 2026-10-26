"""``core.secretbox``, ``core.pii`` and ``core.qr_store`` (customer contract §2.1.1, §2.9.3, §6.4)."""

import base64
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from glowderma.core import qr_store, secretbox
from glowderma.core.pii import mask_email, mask_phone, pii_ref


def test_secretbox_round_trip_and_random_nonce() -> None:
    first = secretbox.seal(b"123456", aad=b"42")
    second = secretbox.seal(b"123456", aad=b"42")
    assert first != second  # random 96-bit nonce
    assert "123456" not in first
    assert secretbox.open_(first, aad=b"42") == secretbox.open_(second, aad=b"42") == b"123456"


def test_secretbox_refuses_tampering_wrong_aad_and_another_key() -> None:
    token = secretbox.seal(b"apple-refresh-token", aad=b"identity:7")
    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw[-1] ^= 1
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    with pytest.raises(secretbox.SecretboxError):
        secretbox.open_(tampered, aad=b"identity:7")
    with pytest.raises(secretbox.SecretboxError):
        secretbox.open_(token, aad=b"identity:8")
    with pytest.raises(secretbox.SecretboxError):
        secretbox.open_("not base64 at all!", aad=b"identity:7")
    with pytest.raises(secretbox.SecretboxError):
        secretbox.open_("AAAA", aad=b"identity:7")
    other = base64.urlsafe_b64encode(b"o" * 32).decode().rstrip("=")
    with override_settings(SECRETBOX_KEY=other), pytest.raises(secretbox.SecretboxError):
        secretbox.open_(token, aad=b"identity:7")


@pytest.mark.parametrize("key", ["", "c2hvcnQ", base64.urlsafe_b64encode(b"x" * 33).decode(), "!!!"])
def test_secretbox_requires_exactly_32_key_bytes(key: str) -> None:
    with override_settings(SECRETBOX_KEY=key), pytest.raises(ImproperlyConfigured):
        secretbox.seal(b"x", aad=b"")


def test_pii_ref_is_keyed_and_short() -> None:
    ref = pii_ref("+998901234567")
    assert len(ref) == 16 and int(ref, 16) >= 0
    assert ref == pii_ref("+998901234567")
    assert ref != pii_ref("+998901234568")
    with override_settings(LOG_PII_KEY="another-log-pii-key-at-least-32-characters"):
        assert pii_ref("+998901234567") != ref


def test_masks() -> None:
    assert mask_phone("+998901234567") == "+998 90 *** ** 67"
    assert mask_phone("12345") == "***"
    assert mask_email("malika@gmail.com") == "m***@gmail.com"
    assert mask_email("nope") == "***"


def test_qr_take_is_single_use() -> None:
    assert qr_store.put("a" * 64, 5, ttl=60, max_live=2)
    assert not qr_store.put("a" * 64, 5, ttl=60, max_live=2)  # SET NX
    value = qr_store.take("a" * 64)
    assert value is not None and value.startswith("5:")
    assert qr_store.take("a" * 64) is None
    assert qr_store.take("b" * 64) is None


def test_qr_take_is_single_use_under_two_threads() -> None:
    qr_store.put("c" * 64, 9, ttl=60, max_live=2)
    start = threading.Event()
    results: list[str | None] = []

    def scan() -> None:
        start.wait(10)
        results.append(qr_store.take("c" * 64))

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert len([value for value in results if value is not None]) == 1


def test_qr_max_live_evicts_the_oldest_and_revoke_all_empties() -> None:
    for name in ("1", "2", "3"):
        qr_store.put(name * 64, 11, ttl=60, max_live=2)
    assert qr_store.take("1" * 64) is None  # evicted
    qr_store.put("4" * 64, 12, ttl=60, max_live=2)  # another customer
    assert qr_store.revoke_all(11) == 2
    assert qr_store.take("2" * 64) is None and qr_store.take("3" * 64) is None
    assert qr_store.take("4" * 64) is not None
    assert qr_store.revoke_all(11) == 0


def test_qr_entries_expire() -> None:
    qr_store.put("e" * 64, 3, ttl=1, max_live=2)
    key = qr_store.token_key("e" * 64)
    value, _expiry = qr_store._local.values[key]
    qr_store._local.values[key] = (value, 0.0)
    assert qr_store.take("e" * 64) is None
    with pytest.raises(ValueError, match="positive"):
        qr_store.put("f" * 64, 3, ttl=0, max_live=2)


REDIS_URL = "redis://localhost:6380/15"


@pytest.fixture
def redis_cache() -> Iterator[Any]:
    """The raw-Redis path against the local Redis (DB 15), skipped when Redis is not reachable."""
    client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis is not reachable")
    config = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
    with override_settings(CACHES=config):  # Django's setting_changed receiver rebuilds the cache handlers
        assert type(caches["default"]).__name__ == "RedisCache"
        try:
            yield client
        finally:
            for key in client.scan_iter(f"{qr_store.PREFIX}*"):
                client.delete(key)
    client.close()


def test_qr_store_on_real_redis_uses_literal_keys(redis_cache: Any) -> None:
    assert qr_store.put("r1" * 32, 21, ttl=60, max_live=2)
    assert redis_cache.get(qr_store.token_key("r1" * 32)).decode().startswith("21:")
    assert 0 < redis_cache.ttl(qr_store.token_key("r1" * 32)) <= 60
    assert not qr_store.put("r1" * 32, 21, ttl=60, max_live=2)
    qr_store.put("r2" * 32, 21, ttl=60, max_live=2)
    qr_store.put("r3" * 32, 21, ttl=60, max_live=2)
    assert redis_cache.get(qr_store.token_key("r1" * 32)) is None  # oldest evicted
    assert qr_store.take("r2" * 32) is not None
    assert qr_store.take("r2" * 32) is None
    assert qr_store.revoke_all(21) == 2
    assert redis_cache.get(qr_store.token_key("r3" * 32)) is None
