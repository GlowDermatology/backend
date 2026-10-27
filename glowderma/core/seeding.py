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

import functools
import importlib
import importlib.util
import math
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO

from django.apps import apps
from django.db import connection
from django.db.models import AutoField, BigAutoField, Model
from PIL import Image, ImageDraw

from glowderma.core import timeutils

DEFAULT_ANCHOR = "2026-10-24T12:05:00+05:00"

_MASK = 0xFFFFFFFF


class MockRandom:
    """Python port of the frontend's ``createRandom`` (``frontend/src/data/mock/random.ts``, mulberry32).

    ``Math.imul`` is a 32-bit multiply, ``>>>`` a logical shift and ``|``/``^`` operate on the 32-bit pattern,
    so every intermediate is masked to 32 bits; the final division by 2**32 is exact in IEEE doubles on both
    sides. ``int``/``chance``/``pick``/``shuffle`` therefore return exactly what the TypeScript returns for
    the same seed and draw order, which lets the seeders reproduce the frontend's generated mock rows.
    """

    def __init__(self, seed: int) -> None:
        self.state = seed & _MASK

    def next(self) -> float:
        self.state = (self.state + 0x6D2B79F5) & _MASK
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & _MASK
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & _MASK)) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296

    def int(self, low: int, high: int) -> int:
        return low + math.floor(self.next() * (high - low + 1))

    def chance(self, probability: float) -> bool:
        return self.next() < probability

    def pick[T](self, items: Sequence[T]) -> T:
        return items[math.floor(self.next() * len(items))]

    def shuffle[T](self, items: Sequence[T]) -> list[T]:
        copy = list(items)
        for i in range(len(copy) - 1, 0, -1):
            j = math.floor(self.next() * (i + 1))
            copy[i], copy[j] = copy[j], copy[i]
        return copy


@contextmanager
def pinned_clock(instant: datetime) -> Iterator[None]:
    """Run a service at a design instant: ``timeutils.now`` is a module attribute, as the tests patch it."""
    original: Callable[[], datetime] = timeutils.now
    timeutils.now = lambda: instant
    try:
        yield
    finally:
        timeutils.now = original


_ART_COLOURS: dict[str, tuple[int, int, int]] = {
    "dropper": (214, 178, 196),
    "box": (226, 206, 186),
    "texture": (239, 226, 220),
    "tube": (178, 206, 214),
    "pump": (188, 214, 196),
    "jar": (226, 196, 176),
    "toner": (196, 206, 226),
    "portrait": (222, 214, 206),
    "banner": (206, 222, 214),
}
_TONE_COLOURS: dict[str, tuple[int, int, int]] = {
    "default": (255, 255, 255),
    "night": (52, 48, 86),
    "dark-cap": (48, 48, 52),
}
# Demo photos are shown full width on the website (product pages, specialist cards): 1200×1500 (4:5) so they
# stay sharp on a retina screen; process_image keeps them (≤2400 px).
PLACEHOLDER_WIDTH, PLACEHOLDER_HEIGHT = 1200, 1500


def _shade(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    red, green, blue = (max(0, min(255, round(channel * factor))) for channel in colour)
    return red, green, blue


@functools.lru_cache(maxsize=64)
def placeholder_art(kind: str, tone: str = "default") -> bytes:
    """A WebP per art kind/tone (1200×1500) — the seeders' stand-in for product and profile photos.

    Deterministic bytes for a given ``(kind, tone)`` (cached per process: the seed encodes each pair once),
    so re-running a seeder reproduces the same content hash.
    """
    width, height = PLACEHOLDER_WIDTH, PLACEHOLDER_HEIGHT
    background = _ART_COLOURS.get(kind, (220, 220, 220))
    accent = _TONE_COLOURS.get(tone, _TONE_COLOURS["default"])
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    # a soft lower band and a floor shadow, so the shape does not float on a flat colour
    draw.rectangle((0, round(height * 0.72), width, height), fill=_shade(background, 0.92))
    draw.ellipse((300, 1150, 900, 1250), fill=_shade(background, 0.8))
    if kind == "portrait":
        draw.ellipse((420, 330, 780, 730), fill=accent)  # head
        draw.rounded_rectangle((250, 780, 950, 1220), radius=220, fill=accent)  # shoulders
    else:
        draw.rounded_rectangle((380, 380, 820, 1200), radius=90, fill=accent)  # bottle
        draw.rounded_rectangle((470, 220, 730, 400), radius=30, fill=_shade(accent, 0.85))  # cap
        draw.rounded_rectangle((440, 640, 760, 900), radius=24, fill=_shade(background, 1.04))  # label
    output = BytesIO()
    image.save(output, format="WEBP", quality=80, method=4)
    return output.getvalue()


@dataclass(frozen=True)
class SeedContext:
    """What every seeder receives."""

    anchor: datetime
    """The demo "now": the design dataset is dated relative to it (Saturday 24 Oct 2026, 12:05 Tashkent)."""
    keep_open: bool
    """Push seeded open reservations/holds past the real clock so the expiry job does not cancel them."""
    notes: list[str] = field(default_factory=list, compare=False)
    """What a seeder left out on purpose (``"<module>: …"``), printed by ``seed_demo`` after the run."""

    def note(self, message: str) -> None:
        self.notes.append(message)

    def at(self, instant: datetime) -> AbstractContextManager[None]:
        """``with ctx.at(instant):`` — run services with ``timeutils.now()`` pinned to a design instant."""
        return pinned_clock(instant)

    def random(self, seed: int) -> MockRandom:
        """The frontend's ``createRandom(seed)``."""
        return MockRandom(seed)

    def media_placeholder(self, kind: str, tone: str = "default") -> bytes:
        return placeholder_art(kind, tone)


Seeder = Callable[[SeedContext], None]
Preflight = Callable[[], str | None]
"""A check run before any seeder: a message when the database is in a state the seeder cannot handle."""

_registry: dict[str, tuple[int, Seeder]] = {}
_TAG = "_glowderma_seeder"
_PREFLIGHT = "_glowderma_seed_preflight"


def register_seeder(
    name: str, *, order: int, preflight: Preflight | None = None
) -> Callable[[Seeder], Seeder]:
    """Register ``name`` at ``order``; ``preflight`` (optional) runs before every seeder of the run (see
    :func:`preflight_problems`) and answers a message when the seeder would fail on this database."""

    def decorate(seeder: Seeder) -> Seeder:
        if name in _registry:
            raise ValueError(f"Seeder {name!r} is already registered")
        _registry[name] = (order, seeder)
        setattr(seeder, _TAG, (name, order))
        if preflight is not None:
            setattr(seeder, _PREFLIGHT, preflight)
        return seeder

    return decorate


def preflight_problems() -> list[str]:
    """The messages of every registered seeder's preflight check (``"<name>: <message>"``), in seeder order;
    ``seed_demo`` refuses to run when the list is not empty."""
    problems: list[str] = []
    for name, _order, seeder in registered_seeders():
        check: Preflight | None = getattr(seeder, _PREFLIGHT, None)
        message = check() if check is not None else None
        if message:
            problems.append(f"{name}: {message}")
    return problems


def registered_seeders() -> list[tuple[str, int, Seeder]]:
    return sorted(
        ((name, order, seeder) for name, (order, seeder) in _registry.items()), key=lambda x: (x[1], x[0])
    )


def discover_seeders() -> list[str]:
    """Import ``<app>.seeds`` for every installed glowderma app that has one; returns the modules found.

    Registration happens at import time, so a module imported while a test had swapped the registry out would
    otherwise stay unregistered for the rest of the process: every tagged seeder of a discovered module is
    (re-)entered into the current registry, which makes discovery idempotent.
    """
    found: list[str] = []
    for config in apps.get_app_configs():
        if not config.name.startswith("glowderma."):
            continue
        module_name = f"{config.name}.seeds"
        if importlib.util.find_spec(module_name) is None:
            continue
        module = importlib.import_module(module_name)
        for attribute in vars(module).values():
            tag = getattr(attribute, _TAG, None)
            if tag is not None and callable(attribute):
                _registry.setdefault(tag[0], (tag[1], attribute))
        found.append(module_name)
    return found


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
