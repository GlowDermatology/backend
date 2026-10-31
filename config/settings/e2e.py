"""Settings of the ops console's browser end-to-end suite (``e2e_admin/``, Playwright).

The local settings with production-like page serving, so template and static mistakes surface in a
browser run:

* ``DEBUG`` off: errors answer the real 404/500 pages, and static files come from ``collectstatic`` output
  through WhiteNoise with the production storage (``CompressedManifestStaticFilesStorage``: a template naming
  a file that does not exist fails the page instead of passing silently);
* ``E2E_CONSOLE_ENABLED``: the only settings module that switches on ``manage.py e2e_console`` (per-test data
  and database assertions of the suite); the production settings never set it;
* ``/media/`` served like the edge serves it on the console's host (``config.urls_e2e``);
* a dedicated database and Redis database from the environment (``e2e_admin/scripts/serve.sh`` passes
  ``DATABASE_URL`` = ``…/glowderma_e2e`` and ``REDIS_URL`` = ``…/9``), shared by the server and the
  ``e2e_console`` calls (lockouts, cached badges and dashboards live in Redis).
"""

from pathlib import Path

from config.env import env_str

from .local import *  # noqa: F403
from .local import BASE_DIR, logging_config

DEBUG = False
E2E_CONSOLE_ENABLED = True
ROOT_URLCONF = "config.urls_e2e"
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
STATIC_ROOT = Path(env_str("E2E_STATIC_ROOT", str(BASE_DIR / "e2e_admin" / ".static")))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
LOGGING = logging_config(env_str("LOG_LEVEL", "WARNING").upper())
