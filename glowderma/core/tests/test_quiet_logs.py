"""Console noise: the 10 s outbox sweep and the container healthcheck stay quiet while they succeed; failures
and runs that did work still reach the log."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from config.settings.base import logging_config
from glowderma.core import outbox
from glowderma.core.logging import QuietCeleryTaskFilter
from glowderma.core.models import OutboxMessage
from glowderma.core.tasks import dispatch_outbox


def gunicorn_logging() -> Any:
    # gunicorn imports fcntl: the access logger is only importable on POSIX (the container, CI).
    pytest.importorskip("gunicorn.glogging", exc_type=ImportError)
    import config.gunicorn_logging

    return config.gunicorn_logging


def record(name: str, level: int, message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, args, None)


@pytest.mark.parametrize(
    ("name", "level", "message", "args"),
    [
        (
            "celery.beat",
            logging.INFO,
            "Scheduler: Sending due task %s (%s)",
            ("dispatch-outbox", "core.dispatch_outbox"),
        ),
        ("celery.worker.strategy", logging.INFO, "Task %s[%s] received", ("core.dispatch_outbox", "abc")),
        (
            "celery.app.trace",
            logging.INFO,
            "Task %s[%s] succeeded in %ss: %s",
            ("core.dispatch_outbox", "a", 0.01, None),
        ),
    ],
)
def test_outbox_sweep_bookkeeping_is_dropped(
    name: str, level: int, message: str, args: tuple[object, ...]
) -> None:
    assert QuietCeleryTaskFilter().filter(record(name, level, message, *args)) is False


@pytest.mark.parametrize(
    "log_record",
    [
        record(
            "celery.app.trace",
            logging.ERROR,
            "Task %s[%s] raised unexpected: %s",
            "core.dispatch_outbox",
            "a",
            "x",
        ),
        record(
            "celery.beat",
            logging.INFO,
            "Scheduler: Sending due task %s (%s)",
            "commerce-poll-bts",
            "commerce.poll_bts",
        ),
        record(
            "celery.app.trace",
            logging.INFO,
            "Task %s[%s] succeeded in %ss: %s",
            "commerce.poll_bts",
            "a",
            0.1,
            [],
        ),
        record("glowderma.core.tasks", logging.INFO, "core.dispatch_outbox did something"),
    ],
)
def test_failures_and_other_tasks_are_kept(log_record: logging.LogRecord) -> None:
    assert QuietCeleryTaskFilter().filter(log_record) is True


def test_console_handler_uses_the_filter() -> None:
    config = logging_config("INFO")
    assert "quiet_celery_tasks" in config["handlers"]["console"]["filters"]


@pytest.mark.parametrize(
    ("path", "status", "quiet"),
    [
        ("/api/v1/health/", 200, True),
        ("/api/v1/health/", 503, False),
        ("/api/v1/health/", None, False),
        ("/api/v1/orders/", 200, False),
        ("/api/v1/orders/", 404, False),
    ],
)
def test_gunicorn_access_quiets_only_successful_health_checks(
    path: str, status: int | None, quiet: bool
) -> None:
    assert gunicorn_logging().is_quiet_access(path, status) is quiet


def test_gunicorn_logger_skips_successful_health_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = gunicorn_logging()
    calls: list[str] = []
    monkeypatch.setattr(
        "gunicorn.glogging.Logger.access",
        lambda self, resp, req, environ, request_time: calls.append(environ["PATH_INFO"]),
    )
    logger = module.HealthCheckQuietLogger.__new__(module.HealthCheckQuietLogger)

    logger.access(SimpleNamespace(status_code=200), None, {"PATH_INFO": "/api/v1/health/"}, None)
    logger.access(SimpleNamespace(status_code=503), None, {"PATH_INFO": "/api/v1/health/"}, None)
    logger.access(SimpleNamespace(status_code=200), None, {"PATH_INFO": "/api/v1/orders/"}, None)

    assert calls == ["/api/v1/health/", "/api/v1/orders/"]


@pytest.mark.django_db
def test_dispatch_logs_a_summary_only_when_it_did_work(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(outbox, "_handlers", {})
    monkeypatch.setattr(outbox, "_dead_handlers", {})
    outbox.register("test.quiet")(lambda message: None)

    with caplog.at_level(logging.INFO, logger="glowderma.core.tasks"):
        dispatch_outbox()
    assert not [r for r in caplog.records if r.name == "glowderma.core.tasks"]

    OutboxMessage.objects.create(topic="test.quiet", payload={}, dedupe_key="test.quiet:1")
    with caplog.at_level(logging.INFO, logger="glowderma.core.tasks"):
        dispatch_outbox()
    assert [r.getMessage() for r in caplog.records if r.name == "glowderma.core.tasks"] == [
        "dispatch_outbox processed 1 message(s)"
    ]
