"""Copy the files of the local ``MEDIA_ROOT`` that the S3 bucket lacks (dry run unless ``--apply``)."""

from typing import Any

from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError, CommandParser

from config.storage import S3_BACKEND


def walk(storage: FileSystemStorage, directory: str = "") -> list[str]:
    directories, files = storage.listdir(directory)
    names = [f"{directory}/{name}" if directory else name for name in files]
    for sub in directories:
        names.extend(walk(storage, f"{directory}/{sub}" if directory else sub))
    return sorted(names)


class Command(BaseCommand):
    help = "Upload the files of MEDIA_ROOT that the default (S3) storage does not hold yet."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--apply", action="store_true", help="upload; without it only list what would be")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.STORAGES["default"]["BACKEND"] != S3_BACKEND:
            # USE_S3 is read at settings import, so it can be given to this one process while the app stays on
            # the disk (the infra's `scripts/manage.sh <env> sync-media` runs it with USE_S3=true).
            raise CommandError(
                "The default storage is the local disk: run this command with USE_S3=true and the S3_* keys."
            )
        source = FileSystemStorage(location=settings.MEDIA_ROOT)
        scanned = uploaded = already = 0
        for name in walk(source):
            scanned += 1
            if default_storage.exists(name):
                already += 1
                continue
            if not options["apply"]:
                self.stdout.write(f"would upload {name}")
                continue
            with source.open(name, "rb") as handle:
                saved = default_storage.save(name, File(handle))
            if saved != name:
                raise CommandError(f"{name} was stored as {saved}")
            uploaded += 1
            self.stdout.write(f"uploaded {name}")
        self.stdout.write(f"scanned={scanned} uploaded={uploaded} already={already}")
