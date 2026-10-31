#!/usr/bin/env bash
# The Django servers of the ops console's Playwright suite (playwright.config.ts "webServer").
#
#   serve.sh main  — drop and recreate glowderma_e2e (+ glowderma_e2e_empty), migrate, collectstatic, seed the
#                    demo data, then serve on E2E_PORT (default 8770);
#   serve.sh gate  — wait for main's empty database, then serve it on E2E_GATE_PORT (default 8771) with
#                    OPS_ADMIN_ALLOWED_NETS=10.0.0.0/8 behind a trusted proxy 127.0.0.1 (the network-gate and
#                    empty-database tests).
#
# Both use config.settings.e2e (DEBUG off, production static storage, e2e_console enabled).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="${E2E_PYTHON:-$ROOT/.venv/bin/python}"
PG="${E2E_PG_URL:-postgres://glowderma:glowderma@localhost:5433}"
REDIS="${E2E_REDIS_URL:-redis://localhost:6380}"
STATE="$ROOT/e2e_admin/.state"
export DJANGO_SETTINGS_MODULE=config.settings.e2e PYTHONUNBUFFERED=1
manage() { "$PY" manage.py "$@"; }

case "${1:-}" in
  main)
    export DATABASE_URL="$PG/glowderma_e2e" REDIS_URL="$REDIS/9" CELERY_BROKER_URL="$REDIS/10"
    rm -rf "$STATE" && mkdir -p "$STATE"
    manage e2e_console reset-db --data '{"names": ["glowderma_e2e"]}' >/dev/null
    manage migrate --noinput -v 0
    # A copy of the freshly migrated database: what a new deployment looks like (no demo data).
    manage e2e_console reset-db --data '{"names": ["glowderma_e2e_empty"], "template": "glowderma_e2e"}' >/dev/null
    touch "$STATE/empty-ready"
    manage collectstatic --noinput -v 0
    manage e2e_console clear-cache --data '{"all": true}' >/dev/null
    manage seed_demo --force >"$STATE/seed.log" 2>&1
    manage seed_ops_demo >>"$STATE/seed.log" 2>&1
    manage e2e_console seed-coverage >>"$STATE/seed.log"
    touch "$STATE/ready"
    exec "$PY" manage.py runserver "127.0.0.1:${E2E_PORT:-8770}" --noreload
    ;;
  gate)
    export DATABASE_URL="$PG/glowderma_e2e_empty" REDIS_URL="$REDIS/11" CELERY_BROKER_URL="$REDIS/12"
    export OPS_ADMIN_ALLOWED_NETS=10.0.0.0/8 TRUSTED_PROXIES=127.0.0.1/32
    for _ in $(seq 1 600); do [ -f "$STATE/empty-ready" ] && break; sleep 0.5; done
    [ -f "$STATE/empty-ready" ] || { echo "main server never prepared the empty database" >&2; exit 1; }
    # collectstatic of the main server may still run: wait for its manifest.
    for _ in $(seq 1 600); do [ -f "$ROOT/e2e_admin/.static/staticfiles.json" ] && [ -f "$STATE/ready" ] && break; sleep 0.5; done
    manage e2e_console clear-cache --data '{"all": true}' >/dev/null
    exec "$PY" manage.py runserver "127.0.0.1:${E2E_GATE_PORT:-8771}" --noreload
    ;;
  *)
    echo "usage: $0 main|gate" >&2
    exit 2
    ;;
esac
