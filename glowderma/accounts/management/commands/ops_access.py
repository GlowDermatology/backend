"""``manage.py ops_access grant|revoke|reset-2fa|status --email EMAIL [--actor EMAIL]``: ops console access.

* ``grant`` — console access (``is_staff``); the user enrols TOTP at the first login (/django-admin/).
* ``revoke`` — no console access; deletes the user's TOTP device and recovery codes and ends their console
  sessions. The Next.js admin panel is not affected (``manage.py deactivatestaff`` ends that).
* ``reset-2fa`` — deletes the TOTP device and recovery codes (lost phone): the next login enrols again.
* ``status`` — access, devices, recovery codes left (read-only).

Audited ``ops_access.granted`` / ``ops_access.revoked`` / ``ops_access.2fa_reset`` (actor: ``--actor`` or
system; reason ``manage.py ops_access``).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.accounts.console import access
from glowderma.accounts.management import staffops

REASON = "manage.py ops_access"


class Command(BaseCommand):
    help = "Grant, revoke or inspect ops console access, or reset a staff member's 2FA."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("operation", choices=["grant", "revoke", "reset-2fa", "status"])
        parser.add_argument("--email", required=True, help="The email of the staff account.")
        parser.add_argument(
            "--actor",
            default="",
            help="The email of the active staff member who asked for it (the audit actor); default: system.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        user = staffops.target_staff(options["email"])
        operation = options["operation"]
        if operation == "status":
            for name, value in vars(access.status(user)).items():
                self.stdout.write(f"{name}: {value}")
            return
        actor = staffops.operator_actor(options["actor"])
        if operation == "grant":
            if not user.is_active:
                raise CommandError(f"{user.email} is deactivated; reactivate the account first")
            if access.grant(user, actor=actor, reason=REASON):
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{user.email} may now use the ops console; 2FA is set up at the first login."
                    )
                )
            else:
                self.stdout.write(f"{user.email} already has ops console access; nothing changed.")
        elif operation == "revoke":
            result = access.revoke(user, actor=actor, reason=REASON)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{user.email} no longer has ops console access "
                    f"({result['totp_devices']} TOTP device(s), {result['recovery_codes']} recovery code(s) "
                    f"deleted, {result['sessions']} session(s) ended)."
                )
            )
        else:
            result = access.reset_two_factor(user, actor=actor, reason=REASON)
            self.stdout.write(
                self.style.SUCCESS(
                    f"2FA of {user.email} reset ({result['totp_devices']} TOTP device(s), "
                    f"{result['recovery_codes']} recovery code(s) deleted); the next login sets it up again."
                )
            )
