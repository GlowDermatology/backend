from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from glowderma.core.seeding import (
    DEFAULT_ANCHOR,
    SeedContext,
    discover_seeders,
    preflight_problems,
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
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Empty every table first (flush: sequences restart, the always-present rows are "
                "re-created by their post_migrate receivers), then seed. For repeatable demo/e2e runs; "
                "never in production."
            ),
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
        if options["reset"]:
            if settings.SETTINGS_MODULE == "config.settings.production":
                raise CommandError("--reset is refused under the production settings.")
            if not (settings.DEBUG or options["force"]):
                raise CommandError("--reset empties the database; it needs DEBUG or --force.")
            call_command("flush", interactive=False, verbosity=0)
            self.stdout.write("Database flushed.")
        with transaction.atomic():
            names: list[str] = []
            if not options["sequences_only"]:
                modules = discover_seeders()
                problems = preflight_problems()
                if problems:
                    raise CommandError("\n".join(["seed_demo cannot seed this database:", *problems]))
                names = run_seeders(SeedContext(anchor=anchor, keep_open=not options["no_keep_open"]))
                self.stdout.write(
                    f"Imported {len(modules)} seed module(s); ran: {', '.join(names) or 'none'}"
                )
            touched = reset_sequences()
        self.stdout.write(self.style.SUCCESS(f"Sequences re-synced: {touched}"))
