"""``assistant.credentials`` and ``assistant.test_credential``: key resolution and its per-version cache,
rotation through an inactive Vault version tested by a worker, rejection, rollback, Vault failures, and that
the key never reaches a row, an audit entry, a Celery argument, an exception or a log line."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from django.core import serializers
from django.test import override_settings

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.assistant import credential_tasks, credentials
from glowderma.assistant.models import ProviderConfig
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.integrations.llm import registry as llm_registry
from glowderma.integrations.secret import Secret
from glowderma.integrations.vault import registry as vault_registry
from glowderma.integrations.vault.ports import VaultNotFound, VaultUnavailable

pytestmark = pytest.mark.django_db

KEY_1 = "AIzaFirstKey-0123456789abcdefghij"
KEY_2 = "AIzaSecondKey-9876543210zyxwvutsr"
BAD_KEY = "invalid-key-rejected-by-the-fake"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    vault_registry.fake_vault().reset()
    llm_registry.fake_content_model().reset()
    credentials.clear_cache()
    yield
    vault_registry.fake_vault().reset()
    credentials.clear_cache()


@pytest.fixture
def superuser() -> Any:
    return StaffUserFactory.create(is_staff=True, is_superuser=True)


def gemini() -> ProviderConfig:
    return ProviderConfig.objects.get(provider="gemini")


def rotate(key: str, actor: Any, capture: Any) -> str:
    with capture(execute=True):
        credentials.start_rotation("gemini", Secret(key), actor=actor)
    config = gemini()
    return "rotated" if config.pending_version is None and config.test_status == "ok" else config.test_status


def everything_stored() -> str:
    """Every row a rotation may touch, serialized: provider rows and audit entries."""
    rows = list(ProviderConfig.objects.all()) + list(AuditEntry.objects.all())
    return str(serializers.serialize("json", rows))


# --- resolution --------------------------------------------------------------------------------------------


def test_no_active_version_is_key_unavailable() -> None:
    with pytest.raises(credentials.KeyUnavailable) as caught:
        credentials.resolve_active_key(gemini())
    assert caught.value.code == "ai_not_configured"


def test_resolution_is_cached_per_provider_and_version(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    vault = vault_registry.fake_vault()
    reads = vault.reads
    first = credentials.resolve_active_key(gemini())
    second = credentials.resolve_active_key(gemini())
    assert first == second == Secret(KEY_1)
    assert vault.reads == reads + 1  # the second resolve came from the cache
    assert repr(first) == str(first) == f"{first}" == "Secret(***)"


@override_settings(AI_KEY_CACHE_SECONDS=0)
def test_a_zero_ttl_reads_vault_every_time(superuser: Any, django_capture_on_commit_callbacks: Any) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    vault = vault_registry.fake_vault()
    reads = vault.reads
    credentials.resolve_active_key(gemini())
    credentials.resolve_active_key(gemini())
    assert vault.reads == reads + 2


def test_two_workers_pick_the_new_version_on_their_next_resolve_without_a_restart(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    worker_a, worker_b = credentials.KeyCache(), credentials.KeyCache()
    assert worker_a.resolve(gemini()) == Secret(KEY_1)
    assert worker_b.resolve(gemini()) == Secret(KEY_1)
    assert rotate(KEY_2, superuser, django_capture_on_commit_callbacks) == "rotated"
    # each worker reads the row afresh for its next job: the new active_version misses its cache
    assert worker_a.resolve(gemini()) == Secret(KEY_2)
    assert worker_b.resolve(gemini()) == Secret(KEY_2)
    assert len(worker_a) == len(worker_b) == 1  # the old version's entry was dropped


def test_a_missing_version_in_vault_is_vault_not_found(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    config = gemini()
    assert config.active_version is not None
    vault_registry.fake_vault().destroy(credentials.vault_path(config), config.active_version)
    with pytest.raises(VaultNotFound):
        credentials.resolve_active_key(config)


# --- rotation ----------------------------------------------------------------------------------------------


def test_rotation_writes_an_inactive_version_then_the_worker_activates_it(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        pending = credentials.start_rotation("gemini", Secret(KEY_1), actor=superuser)
    config = gemini()
    assert (config.active_version, config.pending_version) == (None, pending.version)
    assert config.test_status == enums.CredentialTestStatus.PENDING
    assert vault_registry.fake_vault().read(credentials.vault_path(config), pending.version)[
        "api_key"
    ] == Secret(KEY_1)
    assert len(callbacks) == 1
    callbacks[0]()  # the worker's task (eager)
    config = gemini()
    assert (config.active_version, config.previous_version, config.pending_version) == (
        pending.version,
        None,
        None,
    )
    assert config.test_status == enums.CredentialTestStatus.OK
    assert config.rotated_by_id == superuser.pk
    assert list(AuditEntry.objects.order_by("id").values_list("action", flat=True)) == [
        "assistant.credential_pending",
        "assistant.credential_rotated",
    ]


def test_a_second_rotation_keeps_the_first_key_as_previous(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    rotate(KEY_2, superuser, django_capture_on_commit_callbacks)
    config = gemini()
    assert (config.previous_version, config.active_version) == (1, 2)


def test_a_rejected_key_clears_pending_and_keeps_the_old_key(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    rotate(BAD_KEY, superuser, django_capture_on_commit_callbacks)
    config = gemini()
    assert (config.active_version, config.pending_version) == (1, None)
    assert config.test_status == enums.CredentialTestStatus.FAILED
    assert config.test_error_code == "ai_not_configured"
    assert credentials.resolve_active_key(config) == Secret(KEY_1)
    assert AuditEntry.objects.filter(action="assistant.credential_rejected").count() == 1


def test_the_celery_task_gets_ids_only(
    superuser: Any, django_capture_on_commit_callbacks: Any, monkeypatch: Any
) -> None:
    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(credential_tasks.test_credential, "delay", lambda *args: sent.append(args))
    with django_capture_on_commit_callbacks(execute=True):
        credentials.start_rotation("gemini", Secret(KEY_1), actor=superuser)
    assert sent == [(gemini().pk, 1, superuser.pk)]
    assert all(value is None or isinstance(value, int) for value in sent[0])


def test_the_task_is_registered_under_its_name() -> None:
    from celery import current_app  # type: ignore[import-untyped]

    current_app.loader.import_default_modules()  # what a worker does at start (autodiscover_tasks)
    assert current_app.tasks["assistant.test_credential"].name == credential_tasks.test_credential.name


def test_a_test_is_refused_while_another_is_pending(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=False):
        credentials.start_rotation("gemini", Secret(KEY_1), actor=superuser)
    with pytest.raises(credentials.RotationRefused) as caught:
        credentials.start_rotation("gemini", Secret(KEY_2), actor=superuser)
    assert caught.value.code == "test_pending"


def test_a_stale_task_changes_nothing(superuser: Any, django_capture_on_commit_callbacks: Any) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    assert credentials.finish_test(gemini().pk, 7) == "stale"
    assert gemini().active_version == 1


@pytest.mark.parametrize("key", ["", "   ", "two words", "x" * 600])
def test_malformed_keys_are_refused_before_vault(key: str, superuser: Any) -> None:
    with pytest.raises(credentials.RotationRefused):
        credentials.start_rotation("gemini", Secret(key), actor=superuser)
    assert vault_registry.fake_vault().writes == 0


# --- Vault failures ----------------------------------------------------------------------------------------


def test_vault_down_on_write_changes_nothing_in_postgres(superuser: Any) -> None:
    vault_registry.fake_vault().fail_next(VaultUnavailable("vault down"))
    before = everything_stored()
    with pytest.raises(VaultUnavailable) as caught:
        credentials.start_rotation("gemini", Secret(KEY_1), actor=superuser)
    assert KEY_1 not in str(caught.value) and KEY_1 not in repr(caught.value)
    assert everything_stored() == before
    assert gemini().pending_version is None


def test_vault_down_while_the_worker_reads_rejects_the_rotation(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        credentials.start_rotation("gemini", Secret(KEY_1), actor=superuser)
    vault_registry.fake_vault().fail_next(VaultUnavailable("vault down"))
    callbacks[0]()
    config = gemini()
    assert (config.active_version, config.pending_version, config.test_error_code) == (
        None,
        None,
        "vault_unavailable",
    )


def test_testing_the_active_key_records_the_result(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        credentials.start_active_test("gemini", actor=superuser)
    config = gemini()
    assert config.test_status == enums.CredentialTestStatus.OK
    assert AuditEntry.objects.filter(action="assistant.credential_tested").count() == 1


def test_testing_without_an_active_key_is_refused(superuser: Any) -> None:
    with pytest.raises(credentials.RotationRefused) as caught:
        credentials.start_active_test("gemini", actor=superuser)
    assert caught.value.code == "ai_not_configured"


# --- rollback ----------------------------------------------------------------------------------------------


def test_rollback_makes_the_previous_version_active_again(
    superuser: Any, django_capture_on_commit_callbacks: Any
) -> None:
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    rotate(KEY_2, superuser, django_capture_on_commit_callbacks)
    worker = credentials.KeyCache()
    assert worker.resolve(gemini()) == Secret(KEY_2)
    with django_capture_on_commit_callbacks(execute=True):
        credentials.rollback("gemini", actor=superuser)
    config = gemini()
    assert (config.active_version, config.previous_version) == (1, 2)
    assert config.test_status == enums.CredentialTestStatus.OK  # re-tested by the worker
    assert worker.resolve(config) == Secret(KEY_1)
    assert AuditEntry.objects.filter(action="assistant.credential_rolled_back").count() == 1


def test_rollback_without_a_previous_version_is_refused(superuser: Any) -> None:
    with pytest.raises(credentials.RotationRefused) as caught:
        credentials.rollback("gemini", actor=superuser)
    assert caught.value.code == "no_previous"


# --- the key never leaks -----------------------------------------------------------------------------------


def test_the_key_never_reaches_rows_audit_or_logs(
    superuser: Any, django_capture_on_commit_callbacks: Any, caplog: Any
) -> None:
    caplog.set_level(logging.DEBUG)
    rotate(KEY_1, superuser, django_capture_on_commit_callbacks)
    rotate(BAD_KEY, superuser, django_capture_on_commit_callbacks)
    rotate(KEY_2, superuser, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        credentials.rollback("gemini", actor=superuser)
    credentials.resolve_active_key(gemini())
    stored = everything_stored()
    audit_documents = json.dumps(list(AuditEntry.objects.values("before", "after", "reason")))
    for key in (KEY_1, KEY_2, BAD_KEY):
        assert key not in stored
        assert key not in audit_documents
        assert key not in caplog.text
    assert "assistant.credential_test" in caplog.text  # the outcome is logged, without the key


def test_the_test_goes_through_the_outbox_so_a_broker_outage_loses_nothing(
    superuser: Any, django_capture_on_commit_callbacks: Any, monkeypatch: Any
) -> None:
    from glowderma.core import tasks as core_tasks
    from glowderma.core.models import OutboxMessage

    def broker_down(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(core_tasks.dispatch_outbox, "delay", broker_down)
    with django_capture_on_commit_callbacks(execute=True):
        credentials.start_rotation("gemini", Secret(KEY_1), actor=superuser)  # no 500: logged, kept
    message = OutboxMessage.objects.get(topic="assistant.credential_test")
    assert message.status == "pending"
    assert message.payload == {"config_id": gemini().pk, "version": 1, "actor_id": superuser.pk}
    assert message.dedupe_key == f"assistant.credential_test:{gemini().pk}:v1"
    assert KEY_1 not in json.dumps(message.payload)
    assert gemini().pending_version == 1
    monkeypatch.undo()
    credentials.finish_test(gemini().pk, 1)  # what the delivered message runs
    ProviderConfig.objects.update(test_status=enums.CredentialTestStatus.OK)
    with django_capture_on_commit_callbacks(execute=False):
        credentials.start_active_test("gemini", actor=superuser)
    active = OutboxMessage.objects.exclude(pk=message.pk).get(topic="assistant.credential_test")
    assert active.payload["version"] is None and ":active:" in active.dedupe_key


def test_a_rotation_that_overlaps_another_keeps_the_first_pending_version(
    superuser: Any, django_capture_on_commit_callbacks: Any, monkeypatch: Any
) -> None:
    vault = vault_registry.fake_vault()
    real_write = vault.write

    def write_while_another_rotation_commits(path: str, values: Any) -> int:
        version = real_write(path, values)
        if version == 1:  # the first admin's Vault write; a second admin's whole rotation lands meanwhile
            with django_capture_on_commit_callbacks(execute=False):
                credentials.start_rotation("gemini", Secret(KEY_2), actor=superuser)
        return version

    monkeypatch.setattr(vault, "write", write_while_another_rotation_commits)
    with pytest.raises(credentials.RotationRefused) as caught:
        credentials.start_rotation("gemini", Secret(KEY_1), actor=superuser)
    assert caught.value.code == "test_pending"
    config = gemini()
    assert config.pending_version == 2  # the second admin's key, which the console said is being tested
    assert AuditEntry.objects.filter(action="assistant.credential_pending").count() == 1
