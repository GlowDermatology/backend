def test_celery_app_configured() -> None:
    from config.celery import app

    assert app.main == "glowderma"
    assert app.conf.task_always_eager is True


def test_idempotency_cleanup_is_registered_and_scheduled() -> None:
    from config.celery import app

    app.autodiscover_tasks(force=True)
    assert "core.purge_idempotency_records" in app.tasks
    schedule = app.conf.beat_schedule["purge-idempotency-records"]
    assert schedule["task"] == "core.purge_idempotency_records"
    assert schedule["schedule"].hour == {3}
    assert schedule["schedule"].minute == {0}
    assert str(app.timezone) == "Asia/Tashkent"


def test_celery_hardening_settings() -> None:
    from django.conf import settings

    from config.celery import app

    assert settings.CELERY_TASK_ACKS_LATE is True
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True
    assert settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1
    assert settings.CELERY_TASK_TIME_LIMIT == 600
    assert settings.CELERY_TASK_SOFT_TIME_LIMIT == 540
    assert settings.CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP is True
    assert settings.CELERY_TASK_DEFAULT_QUEUE == "default"
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_time_limit == 600
    assert app.conf.task_soft_time_limit == 540
    assert app.conf.broker_connection_retry_on_startup is True
    assert app.conf.task_default_queue == "default"
    assert app.conf.worker_hijack_root_logger is False


def test_broker_uses_redis_db_1_and_cache_db_0() -> None:
    """AGENTS.md: Redis DB 0 is the Django cache, DB 1 the Celery broker."""
    from django.conf import settings

    assert settings.CELERY_BROKER_URL.endswith("/1")
    assert settings.REDIS_URL.endswith("/0")
