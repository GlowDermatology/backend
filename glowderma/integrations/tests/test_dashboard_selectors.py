"""Read helpers of the ops console: provider rows, the latest job per kind, webhook outcomes by provider."""

from datetime import datetime, timedelta
from typing import Any

import pytest

from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations import selectors
from glowderma.integrations.models import WebhookInbox
from glowderma.integrations.tests.factories import JobFactory, WebhookInboxFactory

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=TASHKENT)


def _result(outcome: str) -> dict[str, Any]:
    return {"status": 200, "body": {}, "outcome": outcome, "error_code": None}


def test_provider_states_reads_every_row_in_one_query(django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(1):
        states = selectors.provider_states()
    assert set(states) == {"billz", "bts", "click", "payme", "eskiz", "fcm"}


def test_latest_jobs_is_the_newest_of_each_kind(django_assert_num_queries: Any) -> None:
    def done(state: str, at: datetime, **extra: Any) -> Any:
        return JobFactory.create(state=state, created_at=at, started_at=at, finished_at=at, **extra)

    done(enums.JobState.SUCCEEDED, NOW - timedelta(hours=2))
    newest = done(enums.JobState.FAILED, NOW - timedelta(hours=1), error={"code": "x", "message": "y"})
    with django_assert_num_queries(1):
        jobs = selectors.latest_jobs()
    assert jobs == {enums.JobKind.BILLZ_IMPORT: newest}


def test_webhook_outcomes_since_count_total_and_failed_per_provider(django_assert_num_queries: Any) -> None:
    since = NOW - timedelta(hours=24)
    WebhookInboxFactory.create(received_at=NOW - timedelta(hours=1), result=_result("accepted"))
    WebhookInboxFactory.create(received_at=NOW - timedelta(hours=2), result=_result("rejected"))
    WebhookInboxFactory.create(received_at=NOW - timedelta(hours=3), result=_result("duplicate"))
    WebhookInboxFactory.create(
        provider=WebhookInbox.Provider.PAYME, received_at=NOW - timedelta(hours=4), result=_result("error")
    )
    WebhookInboxFactory.create(received_at=NOW - timedelta(hours=25), result=_result("rejected"))  # too old
    with django_assert_num_queries(1):
        outcomes = selectors.webhook_outcomes_since(since)
    assert outcomes == {"click": {"total": 3, "failed": 1}, "payme": {"total": 1, "failed": 1}}
