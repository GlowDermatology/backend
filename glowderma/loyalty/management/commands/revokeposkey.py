"""``manage.py revokeposkey <prefix> --actor <staff email>``: the device's key answers 401 from now on."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.loyalty import services


class Command(BaseCommand):
    help = "Revoke a POS device by its key prefix."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("prefix")
        parser.add_argument("--actor", required=True, help="Email of an active staff user.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            device = services.revoke_pos_device(options["prefix"], options["actor"])
        except services.PosDeviceError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"POS device #{device.pk} {device.name} (prefix {device.key_prefix}) revoked.")
