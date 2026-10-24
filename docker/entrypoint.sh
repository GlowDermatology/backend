#!/bin/sh
set -eu

command="${1:-api}"
if [ "$#" -gt 0 ]; then shift; fi
case "$command" in
  api) exec gunicorn config.wsgi --config config/gunicorn.conf.py "$@" ;;
  worker) exec celery -A config worker -l info "$@" ;;
  beat) exec celery -A config beat -l info --schedule /tmp/celerybeat-schedule "$@" ;;
  migrate) exec python manage.py migrate "$@" ;;
  manage) exec python manage.py "$@" ;;
  *) exec "$command" "$@" ;;
esac
