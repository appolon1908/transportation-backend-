#!/bin/sh
set -eu

read_secret_into_env() {
  variable="$1"
  file_variable="${variable}_FILE"
  file_path="$(printenv "$file_variable" 2>/dev/null || true)"
  if [ -z "$file_path" ]; then
    return 0
  fi
  if [ ! -f "$file_path" ] || [ ! -r "$file_path" ]; then
    echo "required secret file for $variable is missing or unreadable" >&2
    exit 78
  fi
  value="$(cat "$file_path")"
  if [ -z "$value" ]; then
    echo "secret file for $variable is empty" >&2
    exit 78
  fi
  case "$value" in
    *'
'*|*''*)
      echo "secret file for $variable contains a newline" >&2
      exit 78
      ;;
  esac
  export "$variable=$value"
}

require_env() {
  variable="$1"
  value="$(printenv "$variable" 2>/dev/null || true)"
  if [ -z "$value" ]; then
    echo "required runtime value $variable is unavailable" >&2
    exit 78
  fi
}

reject_migrator_credential() {
  if [ -n "${MIGRATOR_DATABASE_URL:-}" ] || [ -n "${MIGRATOR_DATABASE_URL_FILE:-}" ]; then
    echo "migrator credential must not be exposed to a long-running service" >&2
    exit 78
  fi
}

mode="${1:-api}"
shift || true

case "$mode" in
  migrate)
    read_secret_into_env MIGRATOR_DATABASE_URL
    read_secret_into_env INGRESS_DATABASE_URL
    read_secret_into_env WORKER_DATABASE_URL
    require_env MIGRATOR_DATABASE_URL
    require_env INGRESS_DATABASE_URL
    require_env WORKER_DATABASE_URL
    export DATABASE_URL="$MIGRATOR_DATABASE_URL"
    alembic upgrade head
    alembic -c alembic-compliance.ini upgrade head
    ;;
  api)
    reject_migrator_credential
    read_secret_into_env DATABASE_URL
    read_secret_into_env INGRESS_DATABASE_URL
    read_secret_into_env WORKER_DATABASE_URL
    read_secret_into_env GATEWAY_SHARED_SECRET
    read_secret_into_env COMPLIANCE_IDENTIFIER_PEPPER
    require_env DATABASE_URL
    require_env INGRESS_DATABASE_URL
    require_env WORKER_DATABASE_URL
    require_env GATEWAY_SHARED_SECRET
    require_env COMPLIANCE_IDENTIFIER_PEPPER
    require_env FORWARDED_ALLOW_IPS
    exec uvicorn app.production_v4:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --proxy-headers \
      --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
    ;;
  worker)
    reject_migrator_credential
    read_secret_into_env DATABASE_URL
    read_secret_into_env INGRESS_DATABASE_URL
    read_secret_into_env WORKER_DATABASE_URL
    require_env DATABASE_URL
    require_env INGRESS_DATABASE_URL
    require_env WORKER_DATABASE_URL
    exec python -m workers.integration_worker "$@"
    ;;
  *)
    echo "unknown entrypoint mode: $mode" >&2
    exit 64
    ;;
esac
