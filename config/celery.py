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
    "loyalty-reconcile-balances": {
        "task": "loyalty.reconcile_balances",
        "schedule": crontab(hour=3, minute=10),
    },
    # M3 Billz catalog sync (docs/integrations/billz.md): an hourly delta and a daily full reconciliation,
    # both through the billz_import job (single-flight with admin imports); no-ops while BILLZ_MODE=fake.
    # Off the hour so they never meet the other jobs' :00 load; the full at 04:23 Tashkent, away from the
    # delta.
    "billz-sync-delta": {"task": "catalog.billz_sync_delta", "schedule": crontab(minute=7)},
    "billz-sync-full": {"task": "catalog.billz_sync_full", "schedule": crontab(hour=4, minute=23)},
}
