import getpass
import sys
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError

from glowderma.accounts.models import StaffUser


class Command(BaseCommand):
    help = "Create a staff account with a validated password."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")
        parser.add_argument("--superuser", action="store_true")
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
        user = StaffUser(email=options["email"].strip().lower(), full_name=options["full_name"])
        try:
            user.full_clean(exclude=["password"])
            validate_password(password, user)
            create = (
                StaffUser.objects.create_superuser if options["superuser"] else StaffUser.objects.create_user
            )
            create(user.email, password, full_name=user.full_name)
        except (ValidationError, IntegrityError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Staff account created."))
