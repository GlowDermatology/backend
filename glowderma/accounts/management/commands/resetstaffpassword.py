"""``manage.py resetstaffpassword <email> [--actor EMAIL] [--password-stdin]``.

Replaces the password and revokes every open session (reason ``password_changed``). Without
``--password-stdin`` a temporary password is generated and printed once (hand it over and let the person
change it under "Parolni o‘zgartirish"); with it, the password read from stdin is validated like
``createstaff``'s. Social identities stay linked. Audited ``staff.password_reset`` (reason
"manage.py resetstaffpassword"; the password itself is never recorded).
"""

import sys
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.accounts import services
from glowderma.accounts.management import staffops

REASON = "manage.py resetstaffpassword"


class Command(BaseCommand):
    help = "Reset a staff account's password (a printed temporary one, or one read from stdin)."

    def add_arguments(self, parser: CommandParser) -> None:
        staffops.add_target_arguments(parser)
        parser.add_argument("--password-stdin", action="store_true", help="Read the new password from stdin.")

    def handle(self, *args: Any, **options: Any) -> None:
        user = staffops.target_staff(options["email"])
        actor = staffops.operator_actor(options["actor"])
        password: str | None = None
        if options["password_stdin"]:
            password = sys.stdin.read()
            if password.endswith("\r\n"):
                password = password[:-2]
            elif password.endswith("\n"):
                password = password[:-1]
            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
        raw, revoked = staffops.run(
            lambda: services.reset_staff_password(
                user, actor=actor, reason=REASON, password=password, refuse_self=False
            )
        )
        self.stdout.write(
            self.style.SUCCESS(f"Password of {user.email} reset; {revoked} session(s) revoked.")
        )
        if password is None:
            self.stdout.write(f"Temporary password (shown once): {raw}")
