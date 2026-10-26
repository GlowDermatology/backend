"""``manage.py setstaffmanager <email> [--off] [--actor EMAIL]``: grant (or, with ``--off``, remove) the
right to manage staff (admin contract §2.16).

The recovery path when no manager can sign in. Removing the role of the last active manager is refused.
Audited ``staff.updated`` (reason "manage.py setstaffmanager").
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from glowderma.accounts import services
from glowderma.accounts.management import staffops

REASON = "manage.py setstaffmanager"


class Command(BaseCommand):
    help = "Make a staff account a manager (or, with --off, a staff member without the manager role)."

    def add_arguments(self, parser: CommandParser) -> None:
        staffops.add_target_arguments(parser)
        parser.add_argument("--off", action="store_true", help="Remove the manager role instead.")

    def handle(self, *args: Any, **options: Any) -> None:
        user = staffops.target_staff(options["email"])
        actor = staffops.operator_actor(options["actor"])
        wanted = not options["off"]
        if user.is_manager == wanted:
            self.stdout.write(
                f"{user.email} {'is' if wanted else 'is not'} a manager already; nothing changed."
            )
            return
        staffops.run(
            lambda: services.update_staff(
                user, {"is_manager": wanted}, actor=actor, reason=REASON, refuse_self=False
            )
        )
        state = "is now a manager" if wanted else "is no longer a manager"
        self.stdout.write(self.style.SUCCESS(f"{user.email} {state}."))
