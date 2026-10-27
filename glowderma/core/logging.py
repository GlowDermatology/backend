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
    """Inject ``request_id`` into every record (never drops a record).

    Precedence: an explicit ``extra={"request_id": ...}`` wins; then the ``request`` Django attaches through
    ``django.utils.log.log_response`` (the ``django.request`` / ``django.security`` 4xx/5xx lines are emitted
    by ``BaseHandler.get_response`` after the middleware chain, i.e. after the ContextVar was reset); then the
    ContextVar bound by ``RequestIdMiddleware``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            attached = getattr(getattr(record, "request", None), "request_id", None)
            record.request_id = attached if isinstance(attached, str) and attached else current_request_id()
        return True
