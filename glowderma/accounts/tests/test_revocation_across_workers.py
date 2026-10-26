"""Revoked sessions are refused by another API worker process through the shared Redis (design spec
"Verification → Required backend tests": revoked-session codes across gunicorn workers via Redis; gap
TEST-auth-revocation-redis).

Worker B is a real second process (``revocation_worker``) with its own Redis client on the development Redis
(localhost:6380, a dedicated database and key prefix); this test process is worker A. B serves a request
first, so the session's ``auth:sid:{sid}`` active entry (≤ 60 s) sits in Redis; A then revokes the session
(the tombstone is written on commit); B's next request is refused with the contract code from Redis alone —
its database access is cut off for that request, and the control call shows that an active entry would have
needed the database. With a per-process cache (LocMem) B would still accept the token for up to 60 s.
Skipped when the development Redis is unreachable.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import pytest
from django.conf import settings
from django.core.cache.backends.redis import RedisCache
from django.db import connection
from django.test import override_settings
from redis.exceptions import RedisError

from glowderma.accounts import services, tokens
from glowderma.accounts.tests import revocation_worker
from glowderma.accounts.tests.factories import (
    AuthSessionFactory,
    CustomerFactory,
    StaffUserFactory,
    device_headers,
    start_test_session,
)
from glowderma.core import qr_store

REDIS_URL = "redis://localhost:6380/13"  # a database of its own: nothing else on the dev Redis uses 13
WAIT_SECONDS = 60


class Worker:
    """The second worker process, driven one command per line."""

    def __init__(self, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
            [sys.executable, "-m", "glowderma.accounts.tests.revocation_worker"],
            cwd=str(settings.BASE_DIR),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.output: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        assert self._result() == {"ready": True}

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.output.append(line)
            if line.startswith(revocation_worker.RESULT_PREFIX):
                self.lines.put(line.removeprefix(revocation_worker.RESULT_PREFIX))

    def _result(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.lines.get(timeout=WAIT_SECONDS)))
        except queue.Empty:
            pytest.fail("the second worker did not answer:\n" + "".join(self.output[-40:]))

    def authenticate(
        self, audience: str, token: str, *, headers: dict[str, Any] | None = None, no_db: bool = False
    ) -> dict[str, Any]:
        assert self.process.stdin is not None
        command = {"audience": audience, "token": token, "headers": headers or {}, "no_db": no_db}
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()
        return self._result()

    def stop(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - the loop ends with stdin
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture
def shared_redis() -> Iterator[RedisCache]:
    """Point this process's default cache at the shared Redis (own key prefix); skip without Redis."""
    prefix = f"gd-revocation-test-{uuid.uuid4().hex}"
    options = {"socket_connect_timeout": 2, "socket_timeout": 2}
    probe = RedisCache(REDIS_URL, {"KEY_PREFIX": prefix, "OPTIONS": options})
    client = probe._cache.get_client(write=True)
    try:
        client.ping()
    except RedisError:
        pytest.skip("Development Redis is unavailable on localhost:6380")
    caches = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": prefix,
            "OPTIONS": options,
        }
    }
    try:
        with override_settings(CACHES=caches):
            yield probe
    finally:
        for key in [*client.scan_iter(match=f"{prefix}:*"), *client.scan_iter(match=f"{qr_store.PREFIX}*")]:
            client.delete(key)
        client.close()


@pytest.fixture
def worker(shared_redis: RedisCache) -> Iterator[Worker]:
    db = connection.settings_dict
    url = "postgres://{user}:{password}@{host}:{port}/{name}".format(
        user=quote(str(db["USER"]), safe=""),
        password=quote(str(db["PASSWORD"]), safe=""),
        host=db["HOST"] or "localhost",
        port=db["PORT"] or 5432,
        name=quote(str(db["NAME"]), safe=""),
    )
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DATABASE_URL": url,
        "DB_POOL": "false",
        "GD_WORKER_REDIS_URL": REDIS_URL,
        "GD_WORKER_KEY_PREFIX": str(shared_redis.key_prefix),
    }
    process = Worker(env)
    try:
        yield process
    finally:
        process.stop()


def sid_entry(redis: RedisCache, sid: object) -> Any:
    """``auth:sid:{sid}`` as a fresh Redis client reads it (neither worker's own connection)."""
    return RedisCache(REDIS_URL, {"KEY_PREFIX": redis.key_prefix}).get(f"auth:sid:{sid}")


@pytest.mark.redis_integration
@pytest.mark.django_db(transaction=True)
def test_a_deactivated_staff_session_is_refused_by_another_worker(
    shared_redis: RedisCache, worker: Worker
) -> None:
    staff = StaffUserFactory.create()
    session = AuthSessionFactory.create(staff=staff)
    token, _ = tokens.issue_access(session)

    assert worker.authenticate("staff", token) == {"ok": staff.pk}
    assert str(sid_entry(shared_redis, session.pk)).startswith("active:")
    # Control: with the active entry, the check needs the database, which a no_db call refuses.
    assert worker.authenticate("staff", token, no_db=True) == {"error": "database"}

    services.deactivate_staff(staff, actor=None)  # worker A; the tombstone is written on commit

    assert sid_entry(shared_redis, session.pk) == "revoked:deactivated"
    assert worker.authenticate("staff", token, no_db=True) == {"code": "SESSION_EXPIRED"}


@pytest.mark.redis_integration
@pytest.mark.django_db(transaction=True)
def test_a_replaced_customer_session_is_refused_by_another_worker(
    shared_redis: RedisCache, worker: Worker
) -> None:
    customer = CustomerFactory.create()
    first = start_test_session(customer)
    token, _ = tokens.issue_access(first.session)
    headers = device_headers()

    assert worker.authenticate("customer", token, headers=headers) == {"ok": customer.pk}
    assert str(sid_entry(shared_redis, first.session.pk)).startswith("active:")

    start_test_session(customer, fid="fid-other-0002")  # a sign-in on another phone replaces the session

    assert sid_entry(shared_redis, first.session.pk) == "revoked:replaced"
    assert worker.authenticate("customer", token, headers=headers, no_db=True) == {"code": "SESSION_REPLACED"}
