import os

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
app = Celery("glowderma")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.conf.beat_schedule = {
    "dispatch-outbox": {"task": "core.dispatch_outbox", "schedule": 10.0},
    "purge-idempotency-records": {
        "task": "core.purge_idempotency_records",
        "schedule": crontab(hour=3, minute=0),
    },
    # design 01 §2.2: fail running jobs with no heartbeat for 15 min (worker lost), unwedging single-flight
    "reap-stale-jobs": {"task": "integrations.reap_stale_jobs", "schedule": 300.0},
    # Scheduled campaigns start when due (design 03 §3.4 / §5: every minute).
    "start-due-campaigns": {"task": "notifications.start_due_campaigns", "schedule": 60.0},
    # Unpaid orders whose reservation ran out are cancelled (design 03 §4.1 row 3 / §5: every minute).
    "commerce-expire-reservations": {"task": "commerce.expire_reservations", "schedule": 60.0},
    # M2 (plan §8): SmsCode rows, done sms.send outbox rows and old customer sessions.
    "accounts-cleanup-auth": {"task": "accounts.cleanup_auth", "schedule": 3600.0},
    # M2 BTS polling fallback (customer contract §2.13.3); the P0 stub returns [] until P2 implements it.
    "commerce-poll-bts": {"task": "commerce.poll_bts", "schedule": 600.0},
    # M3: BTS's region list is refreshed before it goes stale (24 h), so no customer request pays for it.
    "delivery-refresh-bts-regions": {"task": "delivery.refresh_bts_regions", "schedule": 6 * 3600.0},
    # M3 retention (docs/ops/compliance.md): done outbox rows (never dead ones) after OUTBOX_RETENTION_DAYS,
    # raw provider callbacks (WebhookInbox) after WEBHOOK_INBOX_RETENTION_DAYS; batched, at most 5 minutes a
    # night each.
    "core-purge-outbox": {"task": "core.purge_outbox", "schedule": crontab(hour=3, minute=20)},
    "integrations-purge-webhook-inbox": {
        "task": "integrations.purge_webhook_inbox",
        "schedule": crontab(hour=3, minute=25),
    },
    # M3 nightly consistency report K1-K6 (docs/ops/monitoring.md); it also runs the loyalty ledger
    # reconciliation (loyalty.tasks.reconcile_balances_now), which therefore has no beat entry of its own.
    "reporting-consistency-report": {
        "task": "reporting.consistency_report",
        "schedule": crontab(hour=3, minute=40),
    },
    # M3 Billz catalog sync (docs/integrations/billz.md): an hourly delta and a daily full reconciliation,
    # both through the billz_import job (single-flight with admin imports); no-ops while BILLZ_MODE=fake.
    # Off the hour so they never meet the other jobs' :00 load; the full at 04:23 Tashkent, away from the
    # delta.
    "billz-sync-delta": {"task": "catalog.billz_sync_delta", "schedule": crontab(minute=7)},
    "billz-sync-full": {"task": "catalog.billz_sync_full", "schedule": crontab(hour=4, minute=23)},
}
