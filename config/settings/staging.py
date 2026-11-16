"""Staging: the production settings plus what only a staging stack may do (docs/ops/deploy.md §8).

Everything production validates (secrets, hosts, live-provider credentials, refused dev switches) is validated
here too: this module imports production and only adds the staging differences below. Selected by
``DJANGO_SETTINGS_MODULE=config.settings.staging`` (infra ``environments/staging/stack.env``).
"""

from config.env import env_str

from .production import *  # noqa: F403

# Alerts and the ops console badge say STAGING without relying on ALERT_ENV being set in Vault.
ALERT_ENV = env_str("ALERT_ENV", "staging").strip()
OPS_ENVIRONMENT_LABEL = "STAGING"

# Fake adapters that behave as under local/test (core.integration_modes.fake_in_production). Billz: staff can
# create products (synthetic Billz name, price and stock) and run the import without Billz credentials, and
# online selling is on. No effect once BILLZ_MODE=live. Payments, delivery, SMS and OAuth fakes keep the
# production posture.
STAGING_ACTIVE_FAKES = ["billz"]
