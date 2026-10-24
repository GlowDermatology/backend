def test_celery_app_configured() -> None:
    from config.celery import app

    assert app.main == "glowderma"
    assert app.conf.task_always_eager is True
