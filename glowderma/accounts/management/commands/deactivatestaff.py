"""``manage.py deactivatestaff <email> [--actor EMAIL]``: the account can no longer sign in.

Every open session is revoked at once (reason ``deactivated``; an access token is refused on its next
request), social identities stay linked. Refused for the last active manager (make another staff member a
manager first: ``setstaffmanager``). Audited ``staff.deactivated`` (reason "manage.py deactivatestaff").
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from glowderma.accounts import services
from glowderma.accounts.management import staffops

REASON = "manage.py deactivatestaff"


class Command(BaseCommand):
    help = "Deactivate a staff account and revoke its sessions."

    def add_arguments(self, parser: CommandParser) -> None:
        staffops.add_target_arguments(parser)

    def handle(self, *args: Any, **options: Any) -> None:
        user = staffops.target_staff(options["email"])
        actor = staffops.operator_actor(options["actor"])
        if not user.is_active:
            self.stdout.write(f"{user.email} is already inactive; nothing changed.")
            return
        revoked = staffops.run(
            lambda: services.deactivate_staff(user, actor=actor, reason=REASON, refuse_self=False)
        )
        self.stdout.write(self.style.SUCCESS(f"{user.email} deactivated; {revoked} session(s) revoked."))
