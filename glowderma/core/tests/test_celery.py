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
