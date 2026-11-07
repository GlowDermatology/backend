"""notifications.0010 — the campaign approval workflow, data step (between the schema step ``0009`` and the
constraints step ``0011``).

Forward: a ``queued`` campaign (claimed by the old beat, batch 0 enqueued) continues as ``sending`` with
``started_at = COALESCE(started_at, updated_at)``; a ``scheduled`` campaign becomes a ``draft`` that keeps its
``scheduled_at`` as the requested time — **nothing pre-existing is dispatched without a test and an
approval** (deployment note: superusers re-approve the converted drafts).

Reverse (development only): the rows are shaped so that the old ``campaign_state_fields`` CHECK re-added
by the reverse of ``0009`` holds — ``sending`` / ``stopping`` lose their live tallies, ``partially_sent`` /
``failed`` read as ``sent`` with their counters, ``draft`` / ``tested`` become ``scheduled`` (a draft without
a requested time gets a far-future ``scheduled_at`` so the old beat never dispatches it) and a ``cancelled``
row without ``scheduled_at`` takes its ``cancelled_at``.
"""

from django.db import migrations

FORWARD = (
    """
    UPDATE notifications_campaign
       SET state = 'sending', started_at = COALESCE(started_at, updated_at)
     WHERE state = 'queued'
    """,
    "UPDATE notifications_campaign SET state = 'draft' WHERE state = 'scheduled'",
)
REVERSE = (
    "UPDATE notifications_campaign SET sent = NULL, failed = NULL WHERE state = 'sending'",
    """
    UPDATE notifications_campaign SET state = 'sending', sent = NULL, failed = NULL
     WHERE state = 'stopping'
    """,
    """
    UPDATE notifications_campaign
       SET state = 'sent',
           sent_at = COALESCE(sent_at, stopped_at, failed_at, updated_at),
           sent = COALESCE(sent, 0),
           failed = COALESCE(failed, 0)
     WHERE state IN ('partially_sent', 'failed')
    """,
    # a draft without a requested time gets one the old beat never reaches
    """
    UPDATE notifications_campaign
       SET state = 'scheduled', scheduled_at = COALESCE(scheduled_at, TIMESTAMPTZ '9999-12-31 00:00:00+00')
     WHERE state IN ('draft', 'tested')
    """,
    """
    UPDATE notifications_campaign
       SET scheduled_at = COALESCE(scheduled_at, cancelled_at, updated_at)
     WHERE state = 'cancelled'
    """,
)


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0009_campaign_approval_schema"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[statement.strip() for statement in FORWARD],
            reverse_sql=[statement.strip() for statement in REVERSE],
        ),
    ]
