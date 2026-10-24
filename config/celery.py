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
}
