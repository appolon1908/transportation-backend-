#!/bin/sh
set -eu

mode="${1:-api}"
shift || true

case "$mode" in
  migrate)
    alembic upgrade head
    alembic -c alembic-integrations.ini upgrade head
    ;;
  api)
    exec uvicorn app.production:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --proxy-headers \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
    ;;
  worker)
    exec python -m app.integrations.worker "$@"
    ;;
  *)
    echo "unknown entrypoint mode: $mode" >&2
    exit 64
    ;;
esac
