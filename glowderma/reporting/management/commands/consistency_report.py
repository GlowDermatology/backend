"""``manage.py consistency_report [--json]``: run the nightly consistency report now (docs/ops/monitoring.md).

Exactly what the beat task ``reporting.consistency_report`` does — the checks K1–K6 and the loyalty ledger
reconciliation, the ``report.consistency`` audit row, and on a mismatch the ERROR line and the alert — then
prints one line per check (count and sample ids) or, with ``--json``, the whole report. Ids only: the output
holds no personal data.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from glowderma.reporting.tasks import run_consistency_report


class Command(BaseCommand):
    help = "Run the consistency report (K1-K6 + loyalty reconciliation) now; audited like the nightly run."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--json", action="store_true", help="Print the whole report as JSON.")

    def handle(self, *args: Any, **options: Any) -> None:
        report = run_consistency_report()
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return
        for name, check in report["checks"].items():
            if "skipped" in check:
                state = f"skipped ({check['skipped']})"
            elif "error" in check:
                state = f"ERROR ({check['error']})"
            else:
                state = str(check["count"])
            ids = ", ".join(str(value) for value in check.get("ids", []))
            self.stdout.write(f"{name:34} {state}{f'  ids: {ids}' if ids else ''}")
        mismatches = report["mismatches"]
        if mismatches:
            self.stdout.write(f"{len(mismatches)} check(s) need attention (docs/ops/monitoring.md).")
        else:
            self.stdout.write("All checks passed.")
