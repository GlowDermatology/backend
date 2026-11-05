# Runs before any settings module (base, production, local, test, ...): the deployed stack's secrets file
# (DJANGO_SECRETS_FILE, rendered by Vault Agent) becomes environment variables before base.py reads them.
# A no-op when the variable is unset (development, CI, tests). See config/secrets.py.
from config.secrets import load_secrets_file

load_secrets_file()
