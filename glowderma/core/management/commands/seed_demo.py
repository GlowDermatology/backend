from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from glowderma.core.seeding import (
    DEFAULT_ANCHOR,
    SeedContext,
    discover_seeders,
    reset_sequences,
    run_seeders,
)


class Command(BaseCommand):
    help = "Load the demo dataset (every module's seeds.py) and re-sync the id sequences. Idempotent."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--anchor",
            default=DEFAULT_ANCHOR,
            help=f"ISO datetime the dataset is anchored to (default {DEFAULT_ANCHOR}).",
        )
        parser.add_argument(
            "--no-keep-open",
            action="store_true",
            help="Do not move seeded open reservations/holds past the real clock.",
        )
        parser.add_argument(
            "--sequences-only", action="store_true", help="Only re-sync the id sequences; load nothing."
        )
        parser.add_argument(
            "--force", action="store_true", help="Required outside DEBUG (the dataset is demo data)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG and not options["force"]:
            raise CommandError("seed_demo loads demo data; pass --force outside DEBUG settings.")
        try:
            anchor = datetime.fromisoformat(options["anchor"])
        except ValueError as exc:
            raise CommandError("--anchor must be an ISO 8601 datetime") from exc
        if anchor.tzinfo is None:
            raise CommandError("--anchor must carry a timezone offset")
        with transaction.atomic():
            names: list[str] = []
            if not options["sequences_only"]:
                modules = discover_seeders()
                names = run_seeders(SeedContext(anchor=anchor, keep_open=not options["no_keep_open"]))
                self.stdout.write(
                    f"Imported {len(modules)} seed module(s); ran: {', '.join(names) or 'none'}"
                )
            touched = reset_sequences()
        self.stdout.write(self.style.SUCCESS(f"Sequences re-synced: {touched}"))
