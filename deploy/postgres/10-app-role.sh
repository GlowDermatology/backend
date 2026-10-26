#!/bin/sh
# The application's database role (compose.prod.yaml DATABASE_URL connects as glowderma_app).
#
# Run by the postgres image on an empty volume (it executes /docker-entrypoint-initdb.d/*.sh once), and by hand
# on a volume created before this script existed or after POSTGRES_APP_PASSWORD changed:
#   docker compose -f compose.prod.yaml exec postgres /docker-entrypoint-initdb.d/10-app-role.sh
# Idempotent. Details: app-role.psql next to it.
set -eu
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD is not set (compose.prod.yaml passes it from .env)}"
psql -v ON_ERROR_STOP=1 -q --no-psqlrc \
	--username "${POSTGRES_USER:-glowderma}" --dbname "${POSTGRES_DB:-glowderma}" \
	--set app_password="$POSTGRES_APP_PASSWORD" \
	--file "$(dirname "$0")/app-role.psql"
echo "glowderma_app: role, password and ownership in place"
