# Load the Celery app with the Django project so that ``shared_task(...).delay()`` from the API process (the
# outbox dispatch on commit, the Billz import) uses the configured app, not Celery's unconfigured default.
from .celery import app as celery_app

__all__ = ("celery_app",)
