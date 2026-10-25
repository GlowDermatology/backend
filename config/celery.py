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
    "loyalty-reconcile-balances": {
        "task": "loyalty.reconcile_balances",
        "schedule": crontab(hour=3, minute=10),
    },
}
