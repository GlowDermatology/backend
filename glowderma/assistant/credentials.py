"""The AI provider's API key: where it is resolved, rotated, tested and rolled back.

The key lives only in Vault KV v2 (``integrations.vault``) at
``<VAULT_AI_PREFIX>/<ProviderConfig.vault_path>``,
field ``api_key``. PostgreSQL holds its metadata only (:class:`~glowderma.assistant.models.ProviderConfig`:
active / previous / pending version, rotation and test status). In memory the key is always a
:class:`~glowderma.integrations.secret.Secret` (redacted in ``repr``/``str``/logs, never pickled).

**Resolution** (workers): :func:`resolve_active_key` reads the version the *fresh* ``ProviderConfig`` row
names
and caches it per process under ``(provider, active_version)`` for ``AI_KEY_CACHE_SECONDS``. A rotation or a
rollback changes ``active_version`` in PostgreSQL, so every worker's next resolve misses its cache and reads
the new version — no restart, no pub/sub.

**Rotation** (ops console, superuser): :func:`start_rotation` writes the new key as a new Vault version
(outside any transaction: a Vault write cannot roll back), records it as ``pending_version`` and enqueues
:func:`glowderma.assistant.credential_tasks.test_credential` with ids only, through the outbox topic
``assistant.credential_test`` (so a broker outage at commit time only delays the test). The worker (it has
internet egress;
the api process has none) reads that version and calls the provider's ``check()``; :func:`finish_test` then
activates it in one PostgreSQL transaction (``previous = active``, ``active = pending``) with an audit row, or
clears ``pending_version`` and keeps the old key. :func:`rollback` makes the previous version active again.

Audit actions (no key, ever; versions and error codes only): ``assistant.credential_pending``,
``assistant.credential_rotated``, ``assistant.credential_rejected``, ``assistant.credential_tested``,
``assistant.credential_rolled_back``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction

from glowderma.assistant import topics
from glowderma.assistant.models import ProviderConfig
from glowderma.core import audit, enums, outbox, timeutils
from glowderma.integrations.llm import registry as llm_registry
from glowderma.integrations.llm.ports import LLMError
from glowderma.integrations.secret import Secret
from glowderma.integrations.vault import registry as vault_registry
from glowderma.integrations.vault.ports import VaultError, VaultNotFound

logger = logging.getLogger(__name__)

KEY_FIELD = "api_key"
ENTITY_TYPE = "assistant_provider"
NOT_CONFIGURED = "ai_not_configured"
# A pending test older than this no longer blocks a new rotation (the worker was lost).
PENDING_STALE_AFTER = timedelta(minutes=10)
MAX_KEY_LENGTH = 512


class KeyUnavailable(Exception):
    """The provider has no active key version (nothing was ever rotated in, or it was never tested)."""

    code = NOT_CONFIGURED


class RotationRefused(Exception):
    """A rotation / rollback / test that cannot start now; ``code`` names why (console message)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def vault_path(config: ProviderConfig) -> str:
    return f"{settings.VAULT_AI_PREFIX}/{config.vault_path}"


# --- resolution ---------------------------------------------------------------------------------------------


class KeyCache:
    """A process-local cache of resolved keys: ``(provider, version) -> (expires, Secret)``.

    One instance per process (:data:`_cache`); tests build several to play several workers."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], tuple[float, Secret]] = {}
        self._lock = threading.Lock()

    def resolve(self, config: ProviderConfig) -> Secret:
        version = config.active_version
        if version is None:
            raise KeyUnavailable(f"{config.provider}: no active key version")
        slot = (config.provider, version)
        ttl = int(settings.AI_KEY_CACHE_SECONDS)
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(slot)
            if hit is not None and hit[0] > now:
                return hit[1]
        secret = read_version(config, version)
        with self._lock:
            # other versions of this provider are stale from now on
            for other in [key for key in self._entries if key[0] == config.provider and key != slot]:
                del self._entries[other]
            if ttl > 0:
                self._entries[slot] = (now + ttl, secret)
        return secret

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_cache = KeyCache()


def read_version(config: ProviderConfig, version: int) -> Secret:
    """The key stored as ``version`` (``VaultError`` when Vault is down or the version is gone)."""
    values = vault_registry.get_vault().read(vault_path(config), version)
    secret = values.get(KEY_FIELD)
    if secret is None or not secret:
        raise VaultNotFound(f"{vault_path(config)} v{version}: no {KEY_FIELD}")
    return secret


def resolve_active_key(config: ProviderConfig) -> Secret:
    """The active key of ``config`` (pass a freshly read row): cached per ``(provider, active_version)``.

    Raises :class:`KeyUnavailable` (no active version) or a ``VaultError``; never logs or returns the raw
    value."""
    return _cache.resolve(config)


def active_key(provider: str) -> Secret:
    """:func:`resolve_active_key` for the provider's row read now."""
    return resolve_active_key(ProviderConfig.objects.get(provider=provider))


def clear_cache() -> None:
    """Forget every cached key of this process (tests)."""
    _cache.clear()


# --- rotation -----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pending:
    config_id: int
    version: int


def _snapshot(config: ProviderConfig) -> dict[str, Any]:
    return {
        "provider": config.provider,
        "active_version": config.active_version,
        "previous_version": config.previous_version,
        "pending_version": config.pending_version,
    }


def _pending_blocks(config: ProviderConfig) -> bool:
    if config.test_status != enums.CredentialTestStatus.PENDING:
        return False
    started = config.tested_at or config.updated_at
    return started is not None and timeutils.now() - started < PENDING_STALE_AFTER


def _enqueue_test(config: ProviderConfig, version: int | None, actor: Any) -> None:
    """Queue the worker's test (ids only) in the caller's transaction: the outbox delivers it even when the
    broker is down at commit time. A rotation's test is deduplicated per Vault version; an active-key test
    or a rollback's test is a new message each time."""
    payload = {"config_id": config.pk, "version": version, "actor_id": getattr(actor, "pk", None)}
    suffix = f"v{version}" if version is not None else f"active:{uuid.uuid4().hex}"
    dedupe_key = f"{topics.CREDENTIAL_TEST}:{config.pk}:{suffix}"
    outbox.enqueue(topics.CREDENTIAL_TEST, payload, dedupe_key=dedupe_key)


def start_rotation(provider: str, new_key: Secret, *, actor: Any, request: Any = None) -> Pending:
    """Write ``new_key`` as a new (inactive) Vault version and enqueue its test.

    Raises :class:`RotationRefused` (``empty_key``, ``key_too_long``, ``test_pending``) or a ``VaultError``
    (nothing changed in PostgreSQL then)."""
    value = new_key.reveal()
    if not value.strip():
        raise RotationRefused("empty_key")
    if len(value) > MAX_KEY_LENGTH or any(char.isspace() for char in value.strip()):
        raise RotationRefused("key_invalid")
    config = ProviderConfig.objects.get(provider=provider)
    if _pending_blocks(config):
        raise RotationRefused("test_pending")
    version = vault_registry.get_vault().write(vault_path(config), {KEY_FIELD: Secret(value.strip())})
    with transaction.atomic():
        config = ProviderConfig.objects.select_for_update().get(pk=config.pk)
        if _pending_blocks(config):
            # another rotation (or a test) started while we wrote to Vault: keep its pending version; the
            # version we just wrote stays unused (max_versions prunes it), the admin is told to wait
            raise RotationRefused("test_pending")
        before = _snapshot(config)
        config.pending_version = version
        config.test_status = enums.CredentialTestStatus.PENDING
        config.tested_at = timeutils.now()
        config.test_error_code = ""
        config.updated_by = actor
        config.save(
            update_fields=[
                "pending_version",
                "test_status",
                "tested_at",
                "test_error_code",
                "updated_by",
                "updated_at",
            ]
        )
        audit.record(
            action="assistant.credential_pending",
            entity_type=ENTITY_TYPE,
            entity_id=config.provider,
            actor=actor,
            request=request,
            before=before,
            after=_snapshot(config),
        )
        _enqueue_test(config, version, actor)
    logger.info("assistant.credential_pending provider=%s version=%s", config.provider, version)
    return Pending(config.pk, version)


def start_active_test(provider: str, *, actor: Any, request: Any = None) -> None:
    """Enqueue a test of the active key (``check()`` in a worker); the result lands in ``test_status``."""
    with transaction.atomic():
        config = ProviderConfig.objects.select_for_update().get(provider=provider)
        if config.active_version is None:
            raise RotationRefused(NOT_CONFIGURED)
        if _pending_blocks(config):
            raise RotationRefused("test_pending")
        config.test_status = enums.CredentialTestStatus.PENDING
        config.tested_at = timeutils.now()
        config.test_error_code = ""
        config.save(update_fields=["test_status", "tested_at", "test_error_code", "updated_at"])
        _enqueue_test(config, None, actor)


def check_version(config: ProviderConfig, version: int) -> str:
    """Read ``version`` and ask the provider whether it accepts it: ``""`` or the error code."""
    try:
        key = read_version(config, version)
        model = llm_registry.get_content_model(config.provider)
        model.check(api_key=key, model=config.model_id, timeout_seconds=min(int(config.timeout_seconds), 30))
    except (LLMError, VaultError) as exc:
        return str(getattr(exc, "code", "") or "provider_unavailable")
    return ""


def finish_test(config_id: int, version: int | None, *, actor_id: int | None = None) -> str:
    """The worker side of a test: ``version`` = a pending rotation, ``None`` = the active key.

    Returns the outcome (``rotated``, ``rejected``, ``ok``, ``failed``, ``stale``)."""
    config = ProviderConfig.objects.get(pk=config_id)
    target = config.active_version if version is None else version
    if target is None or (version is not None and config.pending_version != version):
        return "stale"  # rotated / rolled back / superseded meanwhile
    error = check_version(config, target)
    actor = _staff(actor_id)
    with transaction.atomic():
        config = ProviderConfig.objects.select_for_update().get(pk=config_id)
        now = timeutils.now()
        before = _snapshot(config)
        if version is None:
            if config.active_version != target:
                return "stale"
            config.test_status = enums.CredentialTestStatus.FAILED if error else enums.CredentialTestStatus.OK
            config.tested_at = now
            config.test_error_code = error
            config.save(update_fields=["test_status", "tested_at", "test_error_code", "updated_at"])
            audit.record(
                action="assistant.credential_tested",
                entity_type=ENTITY_TYPE,
                entity_id=config.provider,
                actor=actor,
                actor_type=None if actor is not None else "system",
                after={"provider": config.provider, "version": target, "error_code": error},
            )
            outcome = "failed" if error else "ok"
        elif config.pending_version != version:
            return "stale"
        elif error:
            config.pending_version = None
            config.test_status = enums.CredentialTestStatus.FAILED
            config.tested_at = now
            config.test_error_code = error
            config.save(
                update_fields=["pending_version", "test_status", "tested_at", "test_error_code", "updated_at"]
            )
            audit.record(
                action="assistant.credential_rejected",
                entity_type=ENTITY_TYPE,
                entity_id=config.provider,
                actor=actor,
                actor_type=None if actor is not None else "system",
                before=before,
                after={**_snapshot(config), "rejected_version": version, "error_code": error},
            )
            outcome = "rejected"
        else:
            config.previous_version = config.active_version
            config.active_version = version
            config.pending_version = None
            config.rotated_at = now
            config.rotated_by = actor
            config.test_status = enums.CredentialTestStatus.OK
            config.tested_at = now
            config.test_error_code = ""
            config.save(
                update_fields=[
                    "previous_version",
                    "active_version",
                    "pending_version",
                    "rotated_at",
                    "rotated_by",
                    "test_status",
                    "tested_at",
                    "test_error_code",
                    "updated_at",
                ]
            )
            audit.record(
                action="assistant.credential_rotated",
                entity_type=ENTITY_TYPE,
                entity_id=config.provider,
                actor=actor,
                actor_type=None if actor is not None else "system",
                before=before,
                after=_snapshot(config),
            )
            outcome = "rotated"
    logger.info(
        "assistant.credential_test provider=%s version=%s outcome=%s code=%s",
        config.provider,
        target,
        outcome,
        error,
    )
    return outcome


def rollback(provider: str, *, actor: Any, request: Any = None) -> ProviderConfig:
    """Make the previous key version active again (and the current one the previous); then test it."""
    with transaction.atomic():
        config = ProviderConfig.objects.select_for_update().get(provider=provider)
        if config.previous_version is None or config.active_version is None:
            raise RotationRefused("no_previous")
        before = _snapshot(config)
        config.active_version, config.previous_version = config.previous_version, config.active_version
        config.pending_version = None
        config.rotated_at = timeutils.now()
        config.rotated_by = actor
        config.test_status = enums.CredentialTestStatus.PENDING
        config.tested_at = timeutils.now()
        config.test_error_code = ""
        config.updated_by = actor
        config.save(
            update_fields=[
                "active_version",
                "previous_version",
                "pending_version",
                "rotated_at",
                "rotated_by",
                "test_status",
                "tested_at",
                "test_error_code",
                "updated_by",
                "updated_at",
            ]
        )
        audit.record(
            action="assistant.credential_rolled_back",
            entity_type=ENTITY_TYPE,
            entity_id=config.provider,
            actor=actor,
            request=request,
            before=before,
            after=_snapshot(config),
        )
        _enqueue_test(config, None, actor)
    return config


def _staff(actor_id: int | None) -> Any:
    if actor_id is None:
        return None
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=actor_id).first()
