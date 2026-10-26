"""``manage.py rotateposkey <prefix> --actor <staff email>``: a new key (shown once) replaces the old one."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.loyalty import services


class Command(BaseCommand):
    help = "Rotate the API key of a POS device and print the new key once."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("prefix")
        parser.add_argument("--actor", required=True, help="Email of an active staff user.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            raw_key = services.rotate_pos_key(options["prefix"], options["actor"])
        except services.PosDeviceError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("New API key (shown once; the old key no longer works):")
        self.stdout.write(raw_key)
