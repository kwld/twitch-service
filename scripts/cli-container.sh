#!/usr/bin/env bash
set -euo pipefail

ENGINE="${1:-docker}"
CONTAINER_NAME="twitch_eventsub_app_dev"
COMPOSE_FILE="docker-compose.dev.yml"

if [[ ! -f .env ]]; then
  echo "Missing .env file. Copy .env.example to .env and configure values." >&2
  exit 1
fi

if [[ "$ENGINE" != "docker" && "$ENGINE" != "podman" ]]; then
  echo "Usage: ./scripts/cli-container.sh [docker|podman]" >&2
  exit 1
fi

if [[ "$ENGINE" == "docker" ]]; then
  if docker ps --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
    exec docker exec -it "${CONTAINER_NAME}" twitch-eventsub-cli console
  fi
  exec docker compose -f "${COMPOSE_FILE}" run --rm app twitch-eventsub-cli console
fi

if podman ps --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  exec podman exec -it "${CONTAINER_NAME}" twitch-eventsub-cli console
fi
exec podman compose -f "${COMPOSE_FILE}" run --rm app twitch-eventsub-cli console
