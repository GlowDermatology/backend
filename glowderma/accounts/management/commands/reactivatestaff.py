"""``manage.py reactivatestaff <email> [--actor EMAIL]``: the account may sign in again.

The sessions revoked at deactivation stay revoked; the password, the linked identities and the manager role
are kept. Audited ``staff.reactivated`` (reason "manage.py reactivatestaff").
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from glowderma.accounts import services
from glowderma.accounts.management import staffops

REASON = "manage.py reactivatestaff"


class Command(BaseCommand):
    help = "Reactivate a deactivated staff account."

    def add_arguments(self, parser: CommandParser) -> None:
        staffops.add_target_arguments(parser)

    def handle(self, *args: Any, **options: Any) -> None:
        user = staffops.target_staff(options["email"])
        actor = staffops.operator_actor(options["actor"])
        if not staffops.run(lambda: services.reactivate_staff(user, actor=actor, reason=REASON)):
            self.stdout.write(f"{user.email} is already active; nothing changed.")
            return
        self.stdout.write(self.style.SUCCESS(f"{user.email} reactivated."))
