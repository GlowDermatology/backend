from typing import Any, Self, cast
from uuid import uuid4

import pytest
from django.core.cache.backends.redis import RedisCache
from redis.exceptions import RedisError

from glowderma.accounts.api import guard


class ExpiringClient:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.commands: list[tuple[str, str, int]] = []

    def add(self, key: str, value: int, timeout: int) -> bool:
        self.values[key], self.ttls[key] = value, timeout
        return True

    def incr(self, key: str, delta: int) -> int:
        # Reproduce expiry between Django's EXISTS and INCR.
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        self.values[key] = delta
        return delta

    def get_client(self, key: str | None = None, *, write: bool = False) -> Self:
        assert write
        return self

    def pipeline(self, *, transaction: bool = True) -> CounterPipeline:
        assert transaction
        return CounterPipeline(self)


class CounterPipeline:
    def __init__(self, client: ExpiringClient) -> None:
        self.client = client
        self.commands: list[tuple[str, str, int]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def incr(self, key: str) -> Self:
        self.commands.append(("incr", key, 1))
        return self

    def expire(self, key: str, timeout: int, *, nx: bool = False) -> Self:
        assert nx
        self.commands.append(("expire", key, timeout))
        return self

    def execute(self) -> list[int]:
        key = self.commands[0][1]
        # Expire immediately before the atomic transaction executes.
        self.client.values.pop(key, None)
        self.client.ttls.pop(key, None)
        self.client.values[key] = 1
        self.client.ttls[key] = self.commands[1][2]
        self.client.commands = self.commands
        return [1, 1]


def test_redis_increment_survives_expire_between_race(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RedisCache("redis://localhost:6380/0", {})
    fake = ExpiringClient()
    backend.__dict__["_cache"] = fake
    monkeypatch.setattr(guard, "cache", backend)
    monkeypatch.setattr(guard, "caches", {"default": backend}, raising=False)
    assert guard._increment("counter", 60) == 1
    raw_key = backend.make_and_validate_key("counter")
    assert fake.ttls.get(raw_key) == 60
    assert [command[0] for command in fake.commands] == ["incr", "expire"]


@pytest.mark.redis_integration
def test_real_redis_counter_always_has_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RedisCache(
        "redis://localhost:6380/0",
        {
            "KEY_PREFIX": f"guard-ttl-test-{uuid4().hex}",
            "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
        },
    )
    client = backend._cache.get_client(write=True)
    try:
        client.ping()
    except RedisError:
        client.close()
        pytest.skip("Development Redis is unavailable on localhost:6380")
    key = backend.make_and_validate_key("counter")
    monkeypatch.setattr(guard, "cache", backend)
    monkeypatch.setattr(guard, "caches", {"default": backend}, raising=False)
    try:
        client.set(key, 5)  # A persistent counter left by the old race is repaired too.
        for expected in range(6, 106):
            assert guard._increment("counter", 60) == expected
            assert 0 < cast(int, client.ttl(key)) <= 60
        client.expire(key, 0)
        assert guard._increment("counter", 60) == 1
        assert 0 < cast(int, client.ttl(key)) <= 60
    finally:
        client.delete(key)
        client.close()
