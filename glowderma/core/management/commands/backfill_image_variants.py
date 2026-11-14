"""Write the thumbnail and medium variants of images stored before they existed (``core.variants``)."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from glowderma.core.variants import backfill


class Command(BaseCommand):
    help = "Write the missing image variants of every stored image (re-runnable)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true", help="Count what would be written; write nothing."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        report = backfill(dry_run=dry_run)
        prefix = "dry run: " if dry_run else ""
        self.stdout.write(
            f"{prefix}checked {report.checked}, created {report.created}, "
            f"missing {len(report.missing_originals)}, failed {len(report.failed)}"
        )
        for name in report.missing_originals:
            self.stdout.write(self.style.WARNING(f"missing original: {name}"))
        for name in report.failed:
            self.stdout.write(self.style.WARNING(f"failed: {name}"))
