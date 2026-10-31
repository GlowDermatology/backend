"""``manage.py compile_locale [--check]``: compile every ``LOCALE_PATHS/*/LC_MESSAGES/*.po`` to its ``.mo``
with the stdlib compiler of :mod:`glowderma.core.po` (no GNU gettext needed). ``--check`` writes nothing and
fails when a committed ``.mo`` does not match its ``.po``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.core.po import compile_po


def catalogs() -> list[Path]:
    found: list[Path] = []
    for root in settings.LOCALE_PATHS:
        found.extend(sorted(Path(root).glob("*/LC_MESSAGES/*.po")))
    return found


class Command(BaseCommand):
    help = "Compile the project's .po catalogs to .mo (stdlib only); --check verifies the committed files."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--check", action="store_true", help="Fail if a .mo is missing or out of date.")

    def handle(self, *args: Any, **options: Any) -> None:
        stale: list[str] = []
        for po in catalogs():
            mo = po.with_suffix(".mo")
            compiled = compile_po(po.read_text(encoding="utf-8"))
            if options["check"]:
                if not mo.exists() or mo.read_bytes() != compiled:
                    stale.append(str(mo))
                continue
            mo.write_bytes(compiled)
            self.stdout.write(f"compiled {po} -> {mo.name}")
        if stale:
            raise CommandError("out of date (run manage.py compile_locale): " + ", ".join(stale))
