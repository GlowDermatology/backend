"""The per-IP failed-authentication counter of the POS key scheme (customer contract §2.10, §1.8; fix pass 1):
the attempt is reserved atomically before any lookup, valid keys give it back, ``Retry-After`` is the rest of
the window and the Redis counter never loses its TTL."""

import threading
from datetime import timedelta
from io import StringIO
from typing import Any, Self, cast
from uuid import uuid4

import pytest
from django.core.cache.backends.redis import RedisCache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from redis.exceptions import RedisError
from rest_framework.exceptions import Throttled
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.models import AuditEntry
from glowderma.loyalty import authentication, qr, seeds
from glowderma.loyalty.models import PosDevice
from glowderma.loyalty.tests.support import at

RESOLVE = "/api/v1/pos/cashback/resolve-qr/"
BAD_KEY = "Api-Key gdpos_wrong000_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
COUNTER = "pos:authfail:127.0.0.1"


def _bad_client() -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=BAD_KEY)
    return client


@pytest.mark.django_db(transaction=True)
def test_concurrent_bad_keys_cannot_pass_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    limit = 20
    for _ in range(limit - 1):
        authentication._reserve(COUNTER)
    lookups: list[str] = []
    go = threading.Event()
    original = authentication.authenticate_pos_key

    def slow_lookup(raw: str) -> PosDevice | None:
        lookups.append(raw)
        go.wait(2)  # keep the lookup open while the other requests arrive
        return original(raw)

    monkeypatch.setattr(authentication, "authenticate_pos_key", slow_lookup)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        request = APIRequestFactory().post(RESOLVE, HTTP_AUTHORIZATION=BAD_KEY)
        try:
            result = authentication.PosKeyAuthentication().authenticate(Request(request))
            outcome = "anonymous" if result is None else "device"
        except Throttled:
            outcome = "throttled"
        finally:
            connection.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    go.set()
    for thread in threads:
        thread.join(timeout=10)
    assert len(lookups) == 1
    assert sorted(outcomes) == ["anonymous"] + ["throttled"] * 9


@pytest.mark.django_db
def test_valid_keys_do_not_consume_the_failure_budget(pos_client: APIClient) -> None:
    bad = _bad_client()
    for _ in range(19):
        assert bad.post(RESOLVE, {"token": "x"}, format="json").status_code == 401
    for _ in range(5):
        assert pos_client.post(RESOLVE, {"token": qr.new_token()}, format="json").status_code == 409
    assert bad.post(RESOLVE, {"token": "x"}, format="json").status_code == 401  # the 20th failure
    assert bad.post(RESOLVE, {"token": "x"}, format="json").status_code == 429


@pytest.mark.django_db
def test_retry_after_is_the_rest_of_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    start = at(10, 26, 12, 0)
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: start)
    bad = _bad_client()
    with override_settings(POS_FAILED_AUTH_PER_IP_PER_MIN=2):
        assert bad.post(RESOLVE, {"token": "x"}, format="json").status_code == 401
        monkeypatch.setattr("glowderma.core.timeutils.now", lambda: start + timedelta(seconds=45))
        assert bad.post(RESOLVE, {"token": "x"}, format="json").status_code == 401
        blocked = bad.post(RESOLVE, {"token": "x"}, format="json")
    assert blocked.status_code == 429
    assert blocked["Retry-After"] == "15"


# --- Redis: the counter never loses its TTL ----------------------------------------------------------------


class ExpiringClient:
    """A Redis client stub whose key expires right before the counter's transaction runs (the race of
    Django's ``EXISTS`` + ``INCR``)."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.commands: list[tuple[str, ...]] = []
        self.scripts: list[tuple[str, str]] = []

    def get_client(self, key: str | None = None, *, write: bool = False) -> Self:
        assert write
        return self

    def pipeline(self, *, transaction: bool = True) -> CounterPipeline:
        assert transaction
        return CounterPipeline(self)

    def eval(self, script: str, numkeys: int, key: str) -> int:
        self.scripts.append((script, key))
        if key not in self.values:
            return 0
        self.values[key] -= 1
        return self.values[key]


class CounterPipeline:
    def __init__(self, client: ExpiringClient) -> None:
        self.client = client
        self.commands: list[tuple[str, ...]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def incr(self, key: str) -> Self:
        self.commands.append(("incr", key))
        return self

    def expire(self, key: str, timeout: int, *, nx: bool = False) -> Self:
        assert nx
        self.commands.append(("expire", key, str(timeout)))
        return self

    def ttl(self, key: str) -> Self:
        self.commands.append(("ttl", key))
        return self

    def execute(self) -> list[int]:
        key = self.commands[0][1]
        self.client.values.pop(key, None)  # expired immediately before the transaction
        self.client.ttls.pop(key, None)
        self.client.values[key] = 1
        self.client.ttls[key] = int(self.commands[1][2])
        self.client.commands = self.commands
        return [1, 1, self.client.ttls[key]]


def test_redis_counter_keeps_its_ttl_when_the_key_expires_mid_race(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RedisCache("redis://localhost:6380/0", {})
    fake = ExpiringClient()
    backend.__dict__["_cache"] = fake
    monkeypatch.setattr(authentication, "caches", {"default": backend})
    assert authentication._reserve(COUNTER) == (1, 60)
    raw_key = backend.make_and_validate_key(COUNTER)
    assert fake.ttls[raw_key] == 60
    assert [command[0] for command in fake.commands] == ["incr", "expire", "ttl"]
    authentication._release(COUNTER)
    assert fake.values[raw_key] == 0
    fake.values.clear()
    authentication._release(COUNTER)  # an ended window: the give-back creates no key
    assert fake.values == {}


@pytest.mark.redis_integration
def test_real_redis_counter_always_has_a_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RedisCache(
        "redis://localhost:6380/0",
        {
            "KEY_PREFIX": f"pos-auth-test-{uuid4().hex}",
            "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
        },
    )
    client = backend._cache.get_client(write=True)
    try:
        client.ping()
    except RedisError:
        client.close()
        pytest.skip("Development Redis is unavailable on localhost:6380")
    key = backend.make_and_validate_key(COUNTER)
    monkeypatch.setattr(authentication, "caches", {"default": backend})
    try:
        client.set(key, 5)  # a counter left without a TTL by the old race is repaired
        for expected in range(6, 30):
            count, left = authentication._reserve(COUNTER)
            assert count == expected and 0 < left <= 60
            assert 0 < cast(int, client.ttl(key)) <= 60
        authentication._release(COUNTER)
        assert int(cast(bytes, client.get(key))) == 28
        client.delete(key)
        authentication._release(COUNTER)
        assert client.exists(key) == 0
        assert authentication._reserve(COUNTER)[0] == 1
        assert 0 < cast(int, client.ttl(key)) <= 60
    finally:
        client.delete(key)
        client.close()


# --- key prefixes are unambiguous (fix pass 1) -------------------------------------------------------------


@pytest.mark.django_db
def test_a_shared_prefix_is_refused_by_the_key_commands(pos_device: PosDevice) -> None:
    staff = StaffUserFactory.create()
    twin = PosDevice.objects.create(name="Kassa 2", key_prefix=pos_device.key_prefix, key_hash="0" * 64)
    for command in ("revokeposkey", "rotateposkey"):
        with pytest.raises(CommandError, match=f"{pos_device.pk}, {twin.pk}"):
            call_command(command, pos_device.key_prefix, "--actor", staff.email, stdout=StringIO())
    assert PosDevice.objects.filter(is_active=True).count() == 2
    assert not AuditEntry.objects.filter(action__startswith="pos.device_").exists()


@pytest.mark.django_db
def test_the_seeder_skips_a_prefix_already_in_use(
    pos_device: PosDevice, caplog: pytest.LogCaptureFixture
) -> None:
    clash = f"gdpos_{pos_device.key_prefix}_another-local-key-0000000000000"
    with override_settings(POS_API_KEYS=[("Kassa 2", clash)]):
        assert seeds.seed_pos_devices() == 0
    assert PosDevice.objects.count() == 1
    assert "pos.seed_prefix_in_use" in caplog.text and clash not in caplog.text
