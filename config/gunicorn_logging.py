"""Gunicorn access logger that leaves the container healthcheck's successful polls out of the console.

The compose healthcheck (and an orchestrator probe) hits ``/api/v1/health/`` every few seconds; its 2xx/3xx
lines carry no information. A failed health check (status >= 400 or unknown) is still logged, as is every
other request.
"""

from typing import Any

from gunicorn.glogging import Logger  # type: ignore[import-untyped]

HEALTH_PATH = "/api/v1/health/"


def is_quiet_access(path: str | None, status_code: int | None) -> bool:
    return path == HEALTH_PATH and status_code is not None and status_code < 400


class HealthCheckQuietLogger(Logger):  # type: ignore[misc]
    def access(self, resp: Any, req: Any, environ: dict[str, Any], request_time: Any) -> None:
        if is_quiet_access(environ.get("PATH_INFO"), getattr(resp, "status_code", None)):
            return
        super().access(resp, req, environ, request_time)
