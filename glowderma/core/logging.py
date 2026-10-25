"""Request-scoped logging context.

``RequestIdMiddleware`` sets ``request_id_var`` for the duration of a request (token-based reset at the end);
``RequestIdFilter`` copies it onto every log record as ``record.request_id`` so the console formatter can
print ``request_id=<X-Request-ID>``. Outside a request (management commands, Celery workers) it is ``"-"``.
"""

import logging
from contextvars import ContextVar, Token

NO_REQUEST_ID = "-"
request_id_var: ContextVar[str] = ContextVar("glowderma_request_id", default=NO_REQUEST_ID)


def bind_request_id(request_id: str) -> Token[str]:
    """Set the current request id; pass the token to ``unbind_request_id`` when the request ends."""
    return request_id_var.set(request_id)


def unbind_request_id(token: Token[str]) -> None:
    request_id_var.reset(token)


def current_request_id() -> str:
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Inject ``request_id`` into every record (never drops a record)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id()
        return True
