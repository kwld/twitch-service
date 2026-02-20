#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8080}"
ENV_FILE=".env"
ENV_EXAMPLE_FILE=".env.example"

REQUIRED_KEYS=(
  "DATABASE_URL"
  "ADMIN_API_KEY"
  "SERVICE_SIGNING_SECRET"
  "TWITCH_CLIENT_ID"
  "TWITCH_CLIENT_SECRET"
)

get_env_value() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true
}

is_missing_or_placeholder() {
  local value="$1"
  if [[ -z "${value}" ]]; then
    return 0
  fi
  if [[ "${value}" == replace_me* ]]; then
    return 0
  fi
  return 1
}

is_loki_enabled() {
  local host port
  host="$(get_env_value "LOKI_HOST")"
  port="$(get_env_value "LOKI_PORT")"
  if is_missing_or_placeholder "${host}" || is_missing_or_placeholder "${port}"; then
    return 1
  fi
  return 0
}

run_migrations_with_retry() {
  local attempts=30
  local delay_seconds=2
  local try=1
  while (( try <= attempts )); do
    if python -m alembic upgrade head; then
      return 0
    fi
    echo "Migration attempt ${try}/${attempts} failed; retrying in ${delay_seconds}s..."
    sleep "${delay_seconds}"
    ((try++))
  done
  echo "Failed to apply database migrations after ${attempts} attempts."
  return 1
}

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}."
  echo "Copy ${ENV_EXAMPLE_FILE} to ${ENV_FILE} and fill required values."
  exit 1
fi

missing=()
for key in "${REQUIRED_KEYS[@]}"; do
  value="$(get_env_value "${key}")"
  if is_missing_or_placeholder "${value}"; then
    missing+=("${key}")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Cannot start dev mode. Missing or placeholder values in ${ENV_FILE}:"
  for key in "${missing[@]}"; do
    echo "  - ${key}"
  done
  echo "Update ${ENV_FILE} using ${ENV_EXAMPLE_FILE} as reference."
  exit 1
fi

ngrok_token="$(get_env_value "NGROK_AUTHTOKEN")"
if [[ -z "${ngrok_token}" ]]; then
  echo "Warning: NGROK_AUTHTOKEN is empty; ngrok tunnel will not be started."
fi

if is_loki_enabled; then
  mkdir -p logs
  docker compose up -d db alloy
else
  docker compose up -d db
fi

if [[ -n "${ngrok_token}" ]]; then
  nohup ngrok http "${PORT}" >/tmp/ngrok.log 2>&1 &
  echo "Started ngrok on port ${PORT}. Logs: /tmp/ngrok.log"
fi

run_migrations_with_retry
uvicorn app.main:app --reload --host 0.0.0.0 --port "${PORT}"
