"""``manage.py createstaff <email> [--full-name NAME] [--manager] [--superuser] [--password-stdin]``.

Creates an active staff account with a validated password (prompted twice, or read from stdin). ``--manager``
lets the account manage staff (admin contract §2.16); the first account of an install without an active
manager is made a manager anyway, and a superuser always is one (decision GST-1). Audited ``staff.created``
(actor ``system``, reason "manage.py createstaff"). ``--full-name`` is trimmed and, when given, needs 2..150
characters like ``PATCH staff/{id}/``; left out, the account's display name is its email.
"""

import getpass
import sys
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.accounts import services
from glowderma.accounts.models import StaffUser

REASON = "manage.py createstaff"


class Command(BaseCommand):
    help = "Create a staff account with a validated password."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")
        parser.add_argument("--superuser", action="store_true")
        parser.add_argument("--manager", action="store_true", help="The account may manage staff.")
        parser.add_argument("--full-name", default="")
        parser.add_argument("--password-stdin", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["password_stdin"]:
            password = sys.stdin.read()
            if password.endswith("\r\n"):
                password = password[:-2]
            elif password.endswith("\n"):
                password = password[:-1]
        else:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords do not match")
        full_name = options["full_name"].strip()
        # The API's 2..150 rule; empty is allowed (the display name falls back to the email).
        if 0 < len(full_name) < 2:
            raise CommandError("--full-name needs at least 2 characters (or leave it out).")
        user = StaffUser(email=options["email"].strip().lower(), full_name=full_name)
        try:
            user.full_clean(exclude=["password"])
            validate_password(password, user)
            created, _ = services.create_staff(
                email=user.email,
                full_name=user.full_name,
                is_manager=bool(options["manager"]),
                is_superuser=bool(options["superuser"]),
                password=password,
                actor=None,
                reason=REASON,
            )
        except (ValidationError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        except ApiValidationError as exc:
            raise CommandError("A staff account with this email already exists.") from exc
        role = "manager" if created.is_manager else "staff member"
        self.stdout.write(self.style.SUCCESS(f"Staff account created ({role})."))
