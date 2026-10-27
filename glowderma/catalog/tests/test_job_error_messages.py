"""Admin contract §2.3 / §2.14: the ``error`` of a failed Billz import job — each code with the message the
code stores, as one table (the table in §2.14 is parsed, so the contract and the code cannot drift)."""

import re
from pathlib import Path

from glowderma.catalog import services as catalog_services
from glowderma.core.api.exceptions import MESSAGES
from glowderma.integrations import services as job_services
from glowderma.integrations.billz.client import BillzAuthError
from glowderma.integrations.billz.ports import BillzListingIncomplete

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "api" / "admin-api-v1.md"
ROW = re.compile(r"^\| `(?P<code>[a-z_]+)`(?: \(M\d\))? \| \"(?P<message>[^\"]+)\" \|", re.M)


def contract_table() -> dict[str, str]:
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index("- `error` of a failed job")
    table = text[start : text.index("\n\n", text.index("| `error.code`", start))]
    return {match["code"]: match["message"] for match in ROW.finditer(table)}


def test_the_failed_job_messages_match_the_code() -> None:
    generic = MESSAGES["provider_unavailable"]
    assert contract_table() == {
        "provider_unavailable": generic,
        BillzListingIncomplete.code: generic,
        BillzAuthError.code: generic,
        catalog_services.BILLZ_FAKE_MODE_CODE: catalog_services.BILLZ_FAKE_MODE,
        job_services.WORKER_LOST_CODE: job_services.WORKER_LOST_MESSAGE,
        "server_error": MESSAGES["server_error"],
    }
    # §2.3 names the same messages
    text = CONTRACT.read_text(encoding="utf-8")
    section = text[text.index("- Error codes of a failed import job") :].split("\n", 1)[0]
    for message in contract_table().values():
        assert f'"{message}"' in section, message
