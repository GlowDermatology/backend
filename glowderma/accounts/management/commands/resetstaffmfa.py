"""``manage.py resetstaffmfa <email> [--actor EMAIL]``: a lost phone, admin panel MFA (feat/admin-ux).

Removes the account's admin panel TOTP device and recovery codes (the ops console's devices stay: that is
``ops_access reset-2fa``) and revokes every open session (reason ``admin_revoked``); the next sign-in needs
the password only, and the person may enable MFA again in Settings. Unlike ``POST staff/{id}/reset-mfa/`` it
may target the actor's own account. Refused when the account has no panel MFA. Audited ``auth.mfa_reset``
(``via`` cli, reason "manage.py resetstaffmfa") and alerted.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from glowderma.accounts import services
from glowderma.accounts.management import staffops

REASON = "manage.py resetstaffmfa"


class Command(BaseCommand):
    help = "Remove a staff account's admin panel MFA (TOTP device, recovery codes) and revoke its sessions."

    def add_arguments(self, parser: CommandParser) -> None:
        staffops.add_target_arguments(parser)

    def handle(self, *args: Any, **options: Any) -> None:
        user = staffops.target_staff(options["email"])
        actor = staffops.operator_actor(options["actor"])
        result = staffops.run(
            lambda: services.reset_staff_mfa(
                user, actor=actor, reason=REASON, refuse_self=False, via="cli", protect_superuser=False
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Admin panel MFA of {user.email} reset: {result['totp_devices']} device(s), "
                f"{result['recovery_codes']} recovery code(s) removed; {result['revoked_count']} session(s) "
                "revoked."
            )
        )
