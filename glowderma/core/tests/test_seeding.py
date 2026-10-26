import sys
from collections.abc import Iterator
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from PIL import Image

from glowderma.core import seeding, timeutils
from glowderma.core.models import AuditEntry, IdempotencyRecord
from glowderma.core.seeding import MockRandom, SeedContext, register_seeder, registered_seeders, run_seeders

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A throwaway registry; ``discover_seeders()`` heals the real one afterwards (see the discovery test)."""
    monkeypatch.setattr(seeding, "_registry", {})
    yield


def test_seeders_run_in_order_then_name(isolated_registry: None) -> None:
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


def test_duplicate_seeder_name_is_rejected(isolated_registry: None) -> None:
    @register_seeder("dup", order=1)
    def first(context: SeedContext) -> None:
        pass

    with pytest.raises(ValueError, match="already registered"):

        @register_seeder("dup", order=2)
        def second(context: SeedContext) -> None:
            pass


def _apps_with_seeds() -> set[str]:
    return {
        f"{config.name}.seeds"
        for config in apps.get_app_configs()
        if config.name.startswith("glowderma.") and (Path(config.path) / "seeds.py").is_file()
    }


def test_discover_seeders_finds_every_seeds_module_and_heals_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _apps_with_seeds()
    assert set(seeding.discover_seeders()) == expected
    assert expected <= set(sys.modules)
    # A registry swapped out after the modules were imported (a test fixture, a reload) is refilled.
    monkeypatch.setattr(seeding, "_registry", {})
    assert set(seeding.discover_seeders()) == expected
    healed = {seeder.__module__ for _, _, seeder in registered_seeders()}
    assert healed == expected
    assert seeding.discover_seeders() == sorted(expected, key=list(seeding.discover_seeders()).index)


def test_mock_random_matches_the_frontend_vectors() -> None:
    # Printed by node from frontend/src/data/mock/random.ts: createRandom(seed).next() × 5.
    vectors = {
        1: [
            0.6270739405881613,
            0.002735721180215478,
            0.5274470399599522,
            0.9810509674716741,
            0.9683778982143849,
        ],
        248: [
            0.925356333842501,
            0.7136159487999976,
            0.1818826668895781,
            0.9284277136903256,
            0.13922672229819,
        ],
        4812: [
            0.7404222865588963,
            0.2791005386970937,
            0.2148161947261542,
            0.7022200578358024,
            0.7818405847065151,
        ],
        485: [
            0.4356456738896668,
            0.5443729809485376,
            0.5117773441597819,
            0.651210536248982,
            0.7351113725453615,
        ],
    }
    for seed, expected in vectors.items():
        random = MockRandom(seed)
        assert [random.next() for _ in range(5)] == expected
    random = MockRandom(97)
    assert [random.int(0, 60), random.int(1, 12), random.int(1, 28)] == [34, 3, 24]
    assert random.chance(0.75) is True
    assert random.pick(["a", "b", "c", "d"]) == "b"
    assert MockRandom(7).shuffle([1, 2, 3, 4, 5, 6]) == [5, 2, 3, 4, 6, 1]
    assert MockRandom(2**40 + 5).next() == MockRandom(5).next()  # seed >>> 0


def test_pinned_clock_and_context_helpers() -> None:
    original = timeutils.now
    instant = datetime(2026, 3, 1, 9, 30, tzinfo=timeutils.TASHKENT)
    context = SeedContext(anchor=ANCHOR, keep_open=False)
    with context.at(instant):
        assert timeutils.now() == instant
    assert timeutils.now is original
    with pytest.raises(RuntimeError), seeding.pinned_clock(instant):
        raise RuntimeError("boom")
    assert timeutils.now is original
    assert context.random(248).next() == MockRandom(248).next()
    art = context.media_placeholder("dropper", "night")
    assert art == seeding.placeholder_art("dropper", "night")
    assert art != seeding.placeholder_art("dropper")
    with Image.open(BytesIO(art)) as image:
        assert image.format == "WEBP"
        assert image.size == (seeding.PLACEHOLDER_SIDE, seeding.PLACEHOLDER_SIDE)
    assert seeding.placeholder_art("unknown-kind", "unknown-tone")  # falls back to grey


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
def test_seed_demo_command_runs_and_validates_anchor(
    isolated_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The command re-discovers the real seeders (the full dataset is exercised by the module seeder tests and
    # the wave-4 end-to-end test); here only the command's own behaviour matters, so discovery is replaced by
    # a probe registration. The command imports the name, hence the patch on the command module.
    seen: list[SeedContext] = []

    def probe(context: SeedContext) -> None:
        seen.append(context)

    def discover_probe() -> list[str]:
        seeding._registry["probe"] = (1, probe)
        return ["probe.seeds"]

    monkeypatch.setattr("glowderma.core.management.commands.seed_demo.discover_seeders", discover_probe)
    out = StringIO()
    with pytest.raises(CommandError, match="--force"):
        call_command("seed_demo", stdout=out)  # pytest-django runs with DEBUG=False
    call_command("seed_demo", force=True, stdout=out)
    assert "Sequences re-synced" in out.getvalue()
    assert "Imported 1 seed module(s); ran: probe" in out.getvalue()
    assert len(seen) == 1 and seen[0].anchor == ANCHOR and seen[0].keep_open
    with pytest.raises(CommandError, match="timezone offset"):
        call_command("seed_demo", anchor="2026-10-24T12:05:00", force=True)
    with pytest.raises(CommandError, match="ISO 8601"):
        call_command("seed_demo", anchor="soon", force=True)


@pytest.mark.django_db(transaction=True)
def test_seed_demo_reset_flushes_then_seeds(
    isolated_registry: None, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from django.contrib.auth import get_user_model

    get_user_model().objects.create_user("leftover@glowderma.uz", "x" * 16)
    AuditEntry.objects.create(actor_type="system", action="seed", entity_type="t", entity_id="1")
    seen: list[int] = []

    def discover_probe() -> list[str]:
        seeding._registry["probe"] = (1, lambda context: seen.append(AuditEntry.objects.count()))
        return ["probe.seeds"]

    monkeypatch.setattr("glowderma.core.management.commands.seed_demo.discover_seeders", discover_probe)
    out = StringIO()
    call_command("seed_demo", reset=True, force=True, stdout=out)
    assert "Database flushed." in out.getvalue()
    assert seen == [0]  # the seeders ran on an empty database
    assert not get_user_model().objects.filter(email="leftover@glowderma.uz").exists()
    settings.SETTINGS_MODULE = "config.settings.production"
    with pytest.raises(CommandError, match="production"):
        call_command("seed_demo", reset=True, force=True)
    settings.SETTINGS_MODULE = "config.settings.test"
    with pytest.raises(CommandError, match="--force"):
        call_command("seed_demo", reset=True)  # pytest-django runs with DEBUG=False: nothing is flushed


def test_preflight_problems_collect_each_registered_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})

    @seeding.register_seeder("quiet", order=1, preflight=lambda: None)
    def quiet(context: seeding.SeedContext) -> None:  # pragma: no cover - never run
        pass

    @seeding.register_seeder("plain", order=2)
    def plain(context: seeding.SeedContext) -> None:  # pragma: no cover - never run
        pass

    @seeding.register_seeder("blocked", order=3, preflight=lambda: "run `seed_demo --reset`")
    def blocked(context: seeding.SeedContext) -> None:  # pragma: no cover - never run
        pass

    assert seeding.preflight_problems() == ["blocked: run `seed_demo --reset`"]
