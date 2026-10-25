"""Registry for demo seeders.

Each module that ships demo data defines ``glowderma/<module>/seeds.py`` and registers one function::

    @register_seeder("catalog", order=40)
    def seed_catalog(context: SeedContext) -> None: ...

``manage.py seed_demo`` imports every installed module's ``seeds`` module, runs the seeders in ``order``
(layer order: lower modules first) inside one transaction, and resets the primary-key sequences afterwards so
rows inserted with explicit ids (orders from 10001, the design customers 1–12) never collide with later ones.
Seeders must be idempotent: they look rows up by their natural keys (phone, slug, billz_id, order id) before
creating them.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from django.apps import apps
from django.db import connection
from django.db.models import AutoField, BigAutoField, Model

DEFAULT_ANCHOR = "2026-10-24T12:05:00+05:00"


@dataclass(frozen=True)
class SeedContext:
    """What every seeder receives."""

    anchor: datetime
    """The demo "now": the design dataset is dated relative to it (Saturday 24 Oct 2026, 12:05 Tashkent)."""
    keep_open: bool
    """Push seeded open reservations/holds past the real clock so the expiry job does not cancel them."""


Seeder = Callable[[SeedContext], None]

_registry: dict[str, tuple[int, Seeder]] = {}


def register_seeder(name: str, *, order: int) -> Callable[[Seeder], Seeder]:
    def decorate(seeder: Seeder) -> Seeder:
        if name in _registry:
            raise ValueError(f"Seeder {name!r} is already registered")
        _registry[name] = (order, seeder)
        return seeder

    return decorate


def registered_seeders() -> list[tuple[str, int, Seeder]]:
    return sorted(
        ((name, order, seeder) for name, (order, seeder) in _registry.items()), key=lambda x: (x[1], x[0])
    )


def discover_seeders() -> list[str]:
    """Import ``<app>.seeds`` for every installed glowderma app that has one; returns the modules imported."""
    imported: list[str] = []
    for config in apps.get_app_configs():
        if not config.name.startswith("glowderma."):
            continue
        module_name = f"{config.name}.seeds"
        if importlib.util.find_spec(module_name) is None:
            continue
        importlib.import_module(module_name)
        imported.append(module_name)
    return imported


def run_seeders(context: SeedContext) -> list[str]:
    """Run every registered seeder in order; returns their names."""
    names: list[str] = []
    for name, _order, seeder in registered_seeders():
        seeder(context)
        names.append(name)
    return names


def _serial_tables() -> Iterator[tuple[str, str]]:
    """(table, pk column) of every managed concrete model with a serial primary key."""
    model: type[Model]
    for model in apps.get_models():
        pk = model._meta.pk
        if pk is None or not isinstance(pk, AutoField | BigAutoField) or pk.column is None:
            continue
        if not model._meta.managed or model._meta.proxy:
            continue
        yield model._meta.db_table, pk.column


def reset_sequences() -> int:
    """Re-sync every serial primary-key sequence with its table (keeps a restarted sequence's start value).

    An empty table leaves the sequence at its configured start (``10001`` for orders); otherwise the next
    value is ``MAX(id) + 1``. Returns the number of sequences touched.
    """
    touched = 0
    with connection.cursor() as cursor:
        for table, column in _serial_tables():
            cursor.execute("SELECT pg_get_serial_sequence(%s, %s)", [table, column])
            row = cursor.fetchone()
            sequence = row[0] if row else None
            if not sequence:
                continue
            cursor.execute(f'SELECT MAX("{column}") FROM "{table}"')  # noqa: S608 - identifiers from Meta
            max_row = cursor.fetchone()
            max_id = max_row[0] if max_row else None
            if max_id is None:
                cursor.execute(
                    "SELECT start_value FROM pg_sequences WHERE schemaname = current_schema() "
                    "AND sequencename = %s",
                    [sequence.split(".")[-1].strip('"')],
                )
                start_row = cursor.fetchone()
                start = int(start_row[0]) if start_row and start_row[0] is not None else 1
                cursor.execute("SELECT setval(%s, %s, false)", [sequence, start])
            else:
                cursor.execute("SELECT setval(%s, %s, true)", [sequence, int(max_id)])
            touched += 1
    return touched
