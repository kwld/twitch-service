#!/usr/bin/env bash
set -euo pipefail

ENGINE="${1:-docker}"
BUILD_FLAG="${2:-}"

if [[ "${ENGINE}" != "docker" && "${ENGINE}" != "podman" ]]; then
  echo "Usage: ./scripts/prod-container.sh [docker|podman] [--build]" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  if [[ ! -f .env.example ]]; then
    echo "Missing .env.example; cannot bootstrap .env." >&2
    exit 1
  fi
  cp .env.example .env
  echo "Created .env from .env.example"
fi

get_env() {
  local key="$1"
  local value
  value="$(grep -E "^${key}=" .env | tail -n1 | cut -d= -f2- || true)"
  echo "${value}"
}

set_env() {
  local key="$1"
  local value="$2"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    $0 ~ ("^" k "=") { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' .env > .env.tmp
  mv .env.tmp .env
}

random_hex() {
  local bytes="${1:-32}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "${bytes}"
    return
  fi
  head -c "${bytes}" /dev/urandom | od -An -tx1 | tr -d ' \n'
}

set_env "APP_ENV" "prod"

POSTGRES_DB="$(get_env POSTGRES_DB)"
POSTGRES_USER="$(get_env POSTGRES_USER)"
POSTGRES_PASSWORD="$(get_env POSTGRES_PASSWORD)"
POSTGRES_DB="${POSTGRES_DB:-twitch_eventsub}"
POSTGRES_USER="${POSTGRES_USER:-twitch}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-twitch}"

set_env "DATABASE_URL" "postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}"

ADMIN_API_KEY="$(get_env ADMIN_API_KEY)"
if [[ -z "${ADMIN_API_KEY}" || "${ADMIN_API_KEY}" == replace_me* ]]; then
  set_env "ADMIN_API_KEY" "$(random_hex 24)"
  echo "Generated ADMIN_API_KEY"
fi

SERVICE_SIGNING_SECRET="$(get_env SERVICE_SIGNING_SECRET)"
if [[ -z "${SERVICE_SIGNING_SECRET}" || "${SERVICE_SIGNING_SECRET}" == replace_me* ]]; then
  set_env "SERVICE_SIGNING_SECRET" "$(random_hex 32)"
  echo "Generated SERVICE_SIGNING_SECRET"
fi

TWITCH_EVENTSUB_WEBHOOK_SECRET="$(get_env TWITCH_EVENTSUB_WEBHOOK_SECRET)"
if [[ -z "${TWITCH_EVENTSUB_WEBHOOK_SECRET}" || "${TWITCH_EVENTSUB_WEBHOOK_SECRET}" == replace_me* ]]; then
  set_env "TWITCH_EVENTSUB_WEBHOOK_SECRET" "$(random_hex 24)"
  echo "Generated TWITCH_EVENTSUB_WEBHOOK_SECRET"
fi

TWITCH_CLIENT_ID="$(get_env TWITCH_CLIENT_ID)"
TWITCH_CLIENT_SECRET="$(get_env TWITCH_CLIENT_SECRET)"
if [[ "${TWITCH_CLIENT_ID}" == replace_me* || "${TWITCH_CLIENT_SECRET}" == replace_me* ]]; then
  echo "Warning: TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET still use placeholder values." >&2
fi

COMPOSE_ARGS=(-f docker-compose.yml up -d --remove-orphans)
if [[ "${BUILD_FLAG}" == "--build" ]]; then
  COMPOSE_ARGS+=(--build)
fi

"${ENGINE}" compose "${COMPOSE_ARGS[@]}"
