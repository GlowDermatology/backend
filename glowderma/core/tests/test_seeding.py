from datetime import datetime
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from glowderma.core import seeding
from glowderma.core.models import AuditEntry, IdempotencyRecord
from glowderma.core.seeding import SeedContext, register_seeder, registered_seeders, run_seeders

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})


def test_seeders_run_in_order_then_name() -> None:
    calls: list[str] = []

    @register_seeder("beta", order=10)
    def beta(context: SeedContext) -> None:
        calls.append("beta")

    @register_seeder("alpha", order=10)
    def alpha(context: SeedContext) -> None:
        calls.append("alpha")

    @register_seeder("core", order=5)
    def core(context: SeedContext) -> None:
        calls.append("core")
        assert context.anchor == ANCHOR and context.keep_open

    assert [name for name, _, _ in registered_seeders()] == ["core", "alpha", "beta"]
    assert run_seeders(SeedContext(anchor=ANCHOR, keep_open=True)) == ["core", "alpha", "beta"]
    assert calls == ["core", "alpha", "beta"]


def test_duplicate_seeder_name_is_rejected() -> None:
    @register_seeder("dup", order=1)
    def first(context: SeedContext) -> None:
        pass

    with pytest.raises(ValueError, match="already registered"):

        @register_seeder("dup", order=2)
        def second(context: SeedContext) -> None:
            pass


def test_discover_seeders_imports_only_existing_modules() -> None:
    assert seeding.discover_seeders() == []  # no module ships seeds.py yet


@pytest.mark.django_db
def test_reset_sequences_continues_after_explicit_ids() -> None:
    AuditEntry.objects.create(id=5000, actor_type="system", action="seed", entity_type="t", entity_id="1")
    assert seeding.reset_sequences() > 0
    entry = AuditEntry.objects.create(actor_type="system", action="seed", entity_type="t", entity_id="2")
    assert entry.pk == 5001


@pytest.mark.django_db
def test_reset_sequences_returns_empty_tables_to_their_start() -> None:
    assert not IdempotencyRecord.objects.exists()
    seeding.reset_sequences()
    record = IdempotencyRecord.objects.create(
        audience="staff",
        actor_id=1,
        method="POST",
        path="/x/",
        key="00000000-0000-4000-8000-000000000001",
        request_hash="0" * 64,
        response_status=200,
        response_body={},
    )
    assert record.pk == 1


@pytest.mark.django_db
def test_seed_demo_command_runs_and_validates_anchor() -> None:
    out = StringIO()
    with pytest.raises(CommandError, match="--force"):
        call_command("seed_demo", stdout=out)  # pytest-django runs with DEBUG=False
    call_command("seed_demo", force=True, stdout=out)
    assert "Sequences re-synced" in out.getvalue()
    with pytest.raises(CommandError, match="timezone offset"):
        call_command("seed_demo", anchor="2026-10-24T12:05:00", force=True)
    with pytest.raises(CommandError, match="ISO 8601"):
        call_command("seed_demo", anchor="soon", force=True)
