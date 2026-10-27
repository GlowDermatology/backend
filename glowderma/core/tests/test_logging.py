import logging
from collections.abc import Iterator

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings

from config.settings.base import logging_config
from glowderma.core.logging import NO_REQUEST_ID, RequestIdFilter, current_request_id
from glowderma.core.middleware import RequestIdMiddleware

SECRET = "test-internal-secret-at-least-32-characters"  # noqa: S105


class Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.addFilter(RequestIdFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def collector() -> Iterator[Collector]:
    handler = Collector()
    logger = logging.getLogger("glowderma.tests.logging")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def view_that_logs(request: HttpRequest) -> HttpResponse:
    logging.getLogger("glowderma.tests.logging").info("inside request")
    return HttpResponse("ok")


def test_record_outside_request_has_dash(collector: Collector) -> None:
    logging.getLogger("glowderma.tests.logging").info("outside")
    assert collector.records[-1].request_id == NO_REQUEST_ID == "-"  # type: ignore[attr-defined]
    assert current_request_id() == "-"


@override_settings(INTERNAL_API_SECRET=SECRET)
def test_record_inside_request_carries_x_request_id(collector: Collector) -> None:
    """§1.2 X-Request-ID "Accepted only with a valid internal secret"; §1.3 X-Request-ID (always):
    a log record emitted inside the request carries the same id the response returns."""
    request = RequestFactory().get(
        "/api/v1/health/", HTTP_X_REQUEST_ID="trace-12345678", HTTP_X_GD_INTERNAL_SECRET=SECRET
    )
    response = RequestIdMiddleware(view_that_logs)(request)
    assert response["X-Request-ID"] == "trace-12345678"
    record = collector.records[-1]
    assert record.getMessage() == "inside request"
    assert record.request_id == "trace-12345678"  # type: ignore[attr-defined]
    assert current_request_id() == "-"


def test_generated_request_id_is_bound_and_reset(collector: Collector) -> None:
    response = RequestIdMiddleware(view_that_logs)(RequestFactory().get("/api/v1/health/"))
    assert collector.records[-1].request_id == response["X-Request-ID"]  # type: ignore[attr-defined]
    assert current_request_id() == "-"


def test_context_is_reset_when_the_view_raises() -> None:
    def boom(request: HttpRequest) -> HttpResponse:
        assert current_request_id() != "-"
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        RequestIdMiddleware(boom)(RequestFactory().get("/"))
    assert current_request_id() == "-"


def test_filter_keeps_explicit_request_id_and_never_drops() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    record.request_id = "explicit"
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "explicit"  # type: ignore[attr-defined]


def test_keyvalue_formatter_prints_request_id() -> None:
    formatter_cfg = settings.LOGGING["formatters"]["keyvalue"]
    formatter = logging.Formatter(formatter_cfg["format"], formatter_cfg["datefmt"])
    record = logging.LogRecord("glowderma.x", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    RequestIdFilter().filter(record)
    line = formatter.format(record)
    assert "level=WARNING logger=glowderma.x request_id=- message=hello world" in line
    assert line.startswith("time=")


def test_logging_config_levels() -> None:
    config = logging_config("INFO")
    assert config["loggers"]["django"]["level"] == "INFO"
    assert config["loggers"]["django.request"]["level"] == "WARNING"
    assert config["loggers"]["django.security"]["level"] == "WARNING"
    assert config["loggers"]["celery"]["level"] == "INFO"
    assert config["loggers"]["glowderma"]["level"] == "INFO"
    assert logging_config("DEBUG")["loggers"]["glowderma"]["level"] == "DEBUG"
    assert config["handlers"]["console"]["filters"] == ["request_id"]
    assert config["filters"]["request_id"]["()"] == "glowderma.core.logging.RequestIdFilter"
    # local settings default the application loggers to DEBUG
    assert settings.LOGGING["loggers"]["glowderma"]["level"] == "DEBUG"


def test_logging_config_rejects_unknown_level() -> None:
    with pytest.raises(ImproperlyConfigured, match="LOG_LEVEL"):
        logging_config("VERBOSE")


def test_django_request_warning_carries_x_request_id(client: Client) -> None:
    """§1.3 X-Request-ID (always) and §1.3 "500 server_error: quote request_id": the django.request line
    Django emits for a 4xx/5xx *response* (after the middleware chain reset the ContextVar) still carries
    the id the response returned, taken from the request attached by django.utils.log.log_response."""
    handler = Collector()
    logger = logging.getLogger("django.request")
    logger.addHandler(handler)
    try:
        response = client.get("/api/v1/does-not-exist/")
    finally:
        logger.removeHandler(handler)
    assert response.status_code == 404
    assert current_request_id() == "-"
    warnings = [
        r for r in handler.records if r.levelno == logging.WARNING and "does-not-exist" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].request_id == response["X-Request-ID"]  # type: ignore[attr-defined]


def test_filter_prefers_explicit_extra_over_attached_request() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    record.request = RequestFactory().get("/")
    record.request.request_id = "from-request"  # type: ignore[attr-defined]
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "from-request"  # type: ignore[attr-defined]
    explicit = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    explicit.request = record.request  # type: ignore[attr-defined]
    explicit.request_id = "explicit"
    RequestIdFilter().filter(explicit)
    assert explicit.request_id == "explicit"  # type: ignore[attr-defined]
    # django.server logs attach a socket/string as ``request``: no request_id there, fall back to "-".
    other = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    other.request = "GET / HTTP/1.1"
    RequestIdFilter().filter(other)
    assert other.request_id == "-"  # type: ignore[attr-defined]
