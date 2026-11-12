"""``manage.py console_messages [--check]``: keep the ops console's English and Russian catalogs in step with
the code (:mod:`glowderma.core.console_catalog`).

Without options it rewrites ``locale/en`` and ``locale/ru`` (``django.po``): every message the console needs,
the existing translations kept, new messages with an empty ``msgstr`` to fill in, obsolete ones dropped. Then
translate the empty entries and run ``manage.py compile_locale``. ``--check`` writes nothing and fails while a
catalog is incomplete, has an obsolete entry or a translation whose placeholders differ from its message."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.core import console_catalog


class Command(BaseCommand):
    help = "Sync (or --check) the ops console's en and ru catalogs with the code's Uzbek messages."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--check", action="store_true", help="Fail unless both catalogs are complete.")

    def handle(self, *args: Any, **options: Any) -> None:
        catalog = console_catalog.collect()
        failed = False
        for language in console_catalog.TARGET_LANGUAGES:
            if not options["check"]:
                console_catalog.sync(language, catalog)
            found = console_catalog.problems(language, catalog)
            total = len(console_catalog.required(language, catalog))
            self.stdout.write(f"{language}: {total} messages, {len(found)} to resolve")
            if options["check"] and found:
                failed = True
                for line in found[:50]:
                    self.stderr.write(f"  {language}: {line}")
        if failed:
            raise CommandError("the console catalogs are not complete (run manage.py console_messages)")
