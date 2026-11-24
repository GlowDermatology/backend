import os

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
app = Celery("glowderma")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
# The AI assistant's key test (glowderma/assistant/credential_tasks.py): ids only, run by the worker.
app.autodiscover_tasks(related_name="credential_tasks")
app.conf.beat_schedule = {
    "dispatch-outbox": {"task": "core.dispatch_outbox", "schedule": 10.0},
    "purge-idempotency-records": {
        "task": "core.purge_idempotency_records",
        "schedule": crontab(hour=3, minute=0),
    },
    # design 01 §2.2: fail running jobs with no heartbeat for 15 min (worker lost), unwedging single-flight
    "reap-stale-jobs": {"task": "integrations.reap_stale_jobs", "schedule": 300.0},
    # feat/ai-assistant: fail AI generation jobs whose worker was lost (no heartbeat for 5 min)
    "assistant-reap-stale-jobs": {"task": "assistant.reap_stale_jobs", "schedule": 120.0},
    # Scheduled campaigns start when due (design 03 §3.4 / §5: every minute).
    "start-due-campaigns": {"task": "notifications.start_due_campaigns", "schedule": 60.0},
    # Campaign approval workflow: a stopped campaign with no open batch left is finalised (every minute).
    "notifications-finalise-stopping": {
        "task": "notifications.finalise_stopping_campaigns",
        "schedule": 60.0,
    },
    # Unpaid orders whose reservation ran out are cancelled (design 03 §4.1 row 3 / §5: every minute).
    "commerce-expire-reservations": {"task": "commerce.expire_reservations", "schedule": 60.0},
    # M2 (plan §8): SmsCode rows, done sms.send outbox rows and old customer sessions.
    "accounts-cleanup-auth": {"task": "accounts.cleanup_auth", "schedule": 3600.0},
    # M2 BTS polling fallback (customer contract §2.13.3); the P0 stub returns [] until P2 implements it.
    "commerce-poll-bts": {"task": "commerce.poll_bts", "schedule": 600.0},
    # M3: BTS's region list is refreshed before it goes stale (24 h), so no customer request pays for it.
    "delivery-refresh-bts-regions": {"task": "delivery.refresh_bts_regions", "schedule": 6 * 3600.0},
    # Yandex Delivery (plan §3.11): unused checkout quotes are deleted a day after they expired.
    "delivery-purge-quotes": {"task": "delivery.purge_quotes", "schedule": crontab(hour=3, minute=35)},
    # Yandex Delivery (plan §3.7): claims/bulk_info over every unsettled claim (the reliable status path;
    # single flight, a no-op while the fake runs under the production settings).
    "commerce-poll-yandex": {"task": "commerce.poll_yandex", "schedule": 60.0},
    # Uzum Checkout (docs/integrations/uzum.md): getOrderStatus of pending and recently failed Uzum payments
    # and of submitting Uzum refunds — the reliable path behind Uzum's unsigned callbacks (≤ 5 attempts).
    "commerce-reconcile-uzum": {"task": "commerce.reconcile_uzum", "schedule": 120.0},
    # Yandex Delivery (plan §3.9): self-closing delivery issues (preparation deadline in working minutes,
    # courier search > 30 min, returns while the warehouse is closed, half-done cancels) and reminders of
    # urgent issues untouched for 30 minutes.
    "commerce-review-delivery-issues": {"task": "commerce.review_delivery_issues", "schedule": 300.0},
    # Yandex Delivery (plan §3.1): courier name / vehicle / tracking link blanked 7 days after a claim ended.
    "delivery-blank-courier-details": {
        "task": "delivery.blank_courier_details",
        "schedule": crontab(hour=3, minute=37),
    },
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
    # M3 Billz catalog sync (docs/integrations/billz.md): a delta every BILLZ_DELTA_SYNC_SECONDS (default 300;
    # gap X-STOCK-FRESHNESS, decision GC-5) and a daily full reconciliation at 04:23 Tashkent, both through
    # the billz_import job (single-flight with admin imports); no-ops while BILLZ_MODE=fake. app.conf (read
    # above) has already loaded the Django settings.
    "billz-sync-delta": {
        "task": "catalog.billz_sync_delta",
        "schedule": float(settings.BILLZ_DELTA_SYNC_SECONDS),
    },
    "billz-sync-full": {"task": "catalog.billz_sync_full", "schedule": crontab(hour=4, minute=23)},
}
