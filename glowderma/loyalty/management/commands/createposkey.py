"""``manage.py createposkey --name "Kassa 1" [--location "Yunusobod"] --actor <staff email>``.

Creates an active cashier terminal and prints its API key **once** (customer contract §2.10); only the SHA-256
of the key is stored. ``--actor`` must be an active staff user: it fills ``created_by`` and the audit actor.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.loyalty import services


class Command(BaseCommand):
    help = "Create a POS device and print its API key once."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--name", required=True)
        parser.add_argument("--location", default="")
        parser.add_argument("--actor", required=True, help="Email of an active staff user.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            device, raw_key = services.create_pos_device(
                options["name"], options["location"], options["actor"]
            )
        except services.PosDeviceError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"POS device #{device.pk} {device.name} (prefix {device.key_prefix}) created.")
        self.stdout.write("API key (shown once, store it in the terminal now):")
        self.stdout.write(raw_key)
