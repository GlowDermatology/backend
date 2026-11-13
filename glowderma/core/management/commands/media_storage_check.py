"""Check the media storage: write, read and delete a probe, then verify every referenced file exists."""

import sys
from typing import Any, NoReturn
from uuid import uuid4

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django.core.management.base import BaseCommand

from glowderma.core.media_storage import missing_names

LISTED = 20


class Command(BaseCommand):
    help = "Probe the default media storage and list database-referenced files it lacks (exit 1 on a problem)"

    def handle(self, *args: Any, **options: Any) -> None:
        backend = type(storages["default"]).__name__
        try:
            probe = default_storage.save(f"healthcheck/{uuid4()}.txt", ContentFile(b"ok"))
            with default_storage.open(probe, "rb") as handle:
                content = handle.read()
            default_storage.delete(probe)
            if content != b"ok":
                self.fail(f"probe read back different content, backend={backend}")
            missing = missing_names(default_storage)
        except Exception as exc:  # noqa: BLE001 - any storage failure is the problem to report
            self.fail(f"storage error backend={backend}: {type(exc).__name__}: {exc}")
        if missing:
            shown = ", ".join(missing[:LISTED])
            self.fail(f"{len(missing)} referenced file(s) missing backend={backend}: {shown}")
        self.stdout.write(f"ok backend={backend}")

    def fail(self, message: str) -> NoReturn:
        self.stderr.write(message)
        sys.exit(1)
