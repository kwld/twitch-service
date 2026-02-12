#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .env ]]; then
  echo "Missing .env file. Copy .env.example to .env and configure values." >&2
  exit 1
fi

REMOTE_HOST="${1:-}"
REMOTE_PATH="${2:-/opt/twitch-eventsub-service}"

if [[ -z "$REMOTE_HOST" ]]; then
  echo "Usage: ./scripts/deploy.sh user@host [/remote/path]" >&2
  exit 1
fi

echo "Syncing project to ${REMOTE_HOST}:${REMOTE_PATH}"
rsync -az --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  --exclude ".pytest_cache" \
  ./ "${REMOTE_HOST}:${REMOTE_PATH}"

echo "Running remote deployment"
ssh "$REMOTE_HOST" "cd ${REMOTE_PATH} && docker compose pull && docker compose build --pull && docker compose up -d --remove-orphans"

echo "Deployment finished."
