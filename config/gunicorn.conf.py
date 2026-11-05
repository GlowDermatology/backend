from config.env import env_int

bind = "0.0.0.0:8000"
# Heartbeat files in memory: the root filesystem is read-only in production.
worker_tmp_dir = "/dev/shm"  # noqa: S108 - per-container memory filesystem, not a shared /tmp
# X-Forwarded-Proto is trusted only from FORWARDED_ALLOW_IPS (the edge's address, set by the deployment),
# which gunicorn reads from the environment itself; request.is_secure() is then true behind TLS.
worker_class = "gthread"
workers = env_int("GUNICORN_WORKERS", 3)
threads = env_int("GUNICORN_THREADS", 4)
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
# Successful healthcheck polls stay out of the access log (config/gunicorn_logging.py).
logger_class = "config.gunicorn_logging.HealthCheckQuietLogger"
# Common log format plus latency and the X-Request-ID response header set by RequestIdMiddleware. The request
# line is method, path and protocol only: the query string never reaches the log, because the BTS webhook
# authenticates with ?token=<BTS_WEBHOOK_SECRET> and admin searches can carry a phone number.
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s '
    "request_id=%({x-request-id}o)s"
)
