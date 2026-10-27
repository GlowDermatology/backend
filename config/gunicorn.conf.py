from config.env import env_int

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = env_int("GUNICORN_WORKERS", 3)
threads = env_int("GUNICORN_THREADS", 4)
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
# Common log format plus latency and the X-Request-ID response header set by RequestIdMiddleware. The request
# line is method, path and protocol only: the query string never reaches the log, because the BTS webhook
# authenticates with ?token=<BTS_WEBHOOK_SECRET> and admin searches can carry a phone number.
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s '
    "request_id=%({x-request-id}o)s"
)
