from config.env import env_int

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = env_int("GUNICORN_WORKERS", 3)
threads = env_int("GUNICORN_THREADS", 4)
