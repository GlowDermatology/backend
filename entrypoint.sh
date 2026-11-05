#!/bin/sh
# Container entrypoint: one image, one process per container, chosen by the first argument.
#
#   api       wait for secrets and the database, run deploy checks, start gunicorn (GUNICORN_WORKERS x THREADS)
#   worker    wait for secrets and the database, start the Celery worker (CELERY_CONCURRENCY processes)
#   beat      wait for secrets, start Celery beat (exactly ONE per environment)
#   migrate   wait for secrets and the database, run deploy checks, apply migrations, exit
#   manage    python manage.py <args>
#   shell     python manage.py shell
#   <other>   exec'd as given
#
# Environment:
#   DJANGO_SECRETS_FILE   JSON secrets rendered by Vault Agent (production); waited for, then loaded by
#                         config/secrets.py. Unset in development.
#   MIGRATE_ON_START      true: `api` migrates before starting (local development only). Production leaves it
#                         false: the deploy runs `migrate` exactly once, in its own container.
#   WAIT_TIMEOUT          seconds to wait for the secrets file and the database (default 300)
set -eu

mode="${1:-api}"
[ "$#" -gt 0 ] && shift
timeout="${WAIT_TIMEOUT:-300}"

log() { echo "entrypoint[$mode]: $*" >&2; }

wait_for_secrets() {
	[ -n "${DJANGO_SECRETS_FILE:-}" ] || return 0
	waited=0
	until [ -s "$DJANGO_SECRETS_FILE" ]; do
		[ "$waited" -ge "$timeout" ] && { log "no secrets at $DJANGO_SECRETS_FILE after ${timeout}s (Vault sealed?)"; exit 1; }
		[ $((waited % 30)) -eq 0 ] && log "waiting for Vault secrets ($DJANGO_SECRETS_FILE)"
		sleep 2
		waited=$((waited + 2))
	done
}

# Loads the settings (which validates every required secret: a missing one fails here, by name) and waits until
# the database accepts a connection.
wait_for_database() {
	WAIT_TIMEOUT="$timeout" python - <<'PY'
import os, sys, time
import django

django.setup()
from django.db import connection

deadline = time.monotonic() + int(os.environ["WAIT_TIMEOUT"])
while True:
    try:
        connection.ensure_connection()
        break
    except Exception as exc:  # noqa: BLE001 - any connection failure means "not yet"
        if time.monotonic() > deadline:
            sys.exit(f"database not reachable: {type(exc).__name__}")
        print(f"waiting for the database ({type(exc).__name__})", file=sys.stderr, flush=True)
        time.sleep(2)
connection.close()
PY
}

case "$mode" in
api)
	wait_for_secrets
	wait_for_database
	if [ "${MIGRATE_ON_START:-false}" = true ]; then
		python manage.py migrate --noinput
	fi
	python manage.py check --deploy
	exec gunicorn config.wsgi --config config/gunicorn.conf.py "$@"
	;;
worker)
	wait_for_secrets
	wait_for_database
	exec celery -A config worker --loglevel INFO --concurrency "${CELERY_CONCURRENCY:-2}" "$@"
	;;
beat)
	wait_for_secrets
	exec celery -A config beat --loglevel INFO --schedule /tmp/celerybeat-schedule "$@"
	;;
migrate)
	wait_for_secrets
	wait_for_database
	python manage.py check --deploy
	exec python manage.py migrate --noinput "$@"
	;;
manage)
	wait_for_secrets
	exec python manage.py "$@"
	;;
shell)
	wait_for_secrets
	exec python manage.py shell "$@"
	;;
*)
	exec "$mode" "$@"
	;;
esac
