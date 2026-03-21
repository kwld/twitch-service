#!/usr/bin/env bash
set -euo pipefail

ENGINE="${1:-docker}"
COMPOSE_FILE="docker-compose.yml"
shift || true

CLI_ARGS=("$@")
if [[ ${#CLI_ARGS[@]} -eq 0 ]]; then
  CLI_ARGS=("console")
fi

if [[ ! -f .env ]]; then
  echo "Missing .env file. Copy .env.example to .env and configure values." >&2
  exit 1
fi

if [[ "$ENGINE" != "docker" && "$ENGINE" != "podman" ]]; then
  echo "Usage: ./scripts/cli-live.sh [docker|podman] [cli args...]" >&2
  exit 1
fi

echo "[twitch-service] Opening CLI against the LIVE stack (${COMPOSE_FILE})." >&2

if [[ "$ENGINE" == "docker" ]]; then
  exec docker compose -f "${COMPOSE_FILE}" exec app twitch-eventsub-cli "${CLI_ARGS[@]}"
fi

exec podman compose -f "${COMPOSE_FILE}" exec app twitch-eventsub-cli "${CLI_ARGS[@]}"
