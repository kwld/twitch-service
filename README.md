# Twitch EventSub Service

Minimal API service that:
- manages Twitch bot OAuth credentials in PostgreSQL,
- maintains deduplicated Twitch EventSub subscriptions,
- receives Twitch EventSub over WebSocket or Webhook,
- forwards events to local services over WebSocket or outgoing webhooks.

## Features
- Async interactive CLI (`twitch-eventsub-cli console`) to:
  - add/list/refresh bot accounts,
  - create service accounts (`client_id`, `client_secret`),
  - regenerate service account secret.
- API for local services to register interest subscriptions.
- In-memory + database interest registry.
- Startup reconciliation:
  - load interests from DB,
  - fetch existing Twitch subscriptions,
  - reuse existing ones when possible,
  - create only missing subscriptions.
- Reconnect support for Twitch EventSub WebSocket.
- Twitch webhook callback verification + HMAC signature validation.

## Quickstart
1. Copy `.env.example` to `.env` and configure values.
2. Install dependencies:
   ```bash
   pip install -e .
   ```
3. Start local Postgres:
   ```bash
   docker compose up -d db
   ```
4. Start API:
   ```bash
   twitch-eventsub-api
   ```
5. Open async CLI:
   ```bash
   twitch-eventsub-cli console
   ```

If `.env` is missing, app/cli exits with an explicit error.

## API Authentication
- Admin endpoints use header: `X-Admin-Key: <ADMIN_API_KEY>`
- Service endpoints use:
  - `X-Client-Id: <client_id>`
  - `X-Client-Secret: <client_secret>`

## Main Endpoints
- `GET /health`
- `POST /webhooks/twitch/eventsub` (Twitch webhook callback)
- `GET /v1/bots` (admin)
- `POST /v1/admin/service-accounts?name=<name>` (admin)
- `GET /v1/admin/service-accounts` (admin)
- `POST /v1/admin/service-accounts/{client_id}/regenerate` (admin)
- `GET /v1/interests` (service)
- `POST /v1/interests` (service)
- `DELETE /v1/interests/{interest_id}` (service)
- `WS /ws/events?client_id=...&client_secret=...` (service)

### Create Interest Payload
```json
{
  "bot_account_id": "uuid",
  "event_type": "channel.online",
  "broadcaster_user_id": "12345",
  "transport": "websocket",
  "webhook_url": null
}
```

For `transport=webhook`, `webhook_url` is required.

## Dev Script
Run everything for local dev (DB + ngrok + reload):
```powershell
./scripts/dev.ps1 -Port 8080
```

## Dev Bundle (Docker/Podman Desktop on Windows)
This repo includes a hot-reload development bundle:
- `Dockerfile.dev`
- `docker-compose.dev.yml`
- `scripts/dev-container.ps1`

### Start with Docker Desktop
```powershell
./scripts/dev-container.ps1 -Engine docker -Build
```

### Start with Podman Desktop
```powershell
./scripts/dev-container.ps1 -Engine podman -Build
```

### Stop
Docker:
```powershell
docker compose -f docker-compose.dev.yml down
```
Podman:
```powershell
podman compose -f docker-compose.dev.yml down
```

Notes:
- `.env` is required; start command fails if missing.
- Code is bind-mounted (`./:/workspace`) and `uvicorn --reload` is enabled.
- `WATCHFILES_FORCE_POLLING=true` is set for reliable reload on Windows mounted volumes.
- Dev bundle includes an `ngrok` container (inspector at `http://localhost:4040`).
- Set `NGROK_AUTHTOKEN` in `.env` to enable ngrok tunnel.
- Full setup guide: `docs/DEV_SETUP.md`.
- LLM/agent usage guide: `docs/LLM_USAGE.md`.

## Upstream EventSub Routing (Both Transports)
The service can use websocket and webhook upstream at the same time.

Configure in `.env`:
- `TWITCH_EVENTSUB_WS_URL`: websocket endpoint.
- `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL`: public HTTPS callback.
- `TWITCH_EVENTSUB_WEBHOOK_SECRET`: webhook secret (10-100 ASCII chars).
- `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES`: comma-separated event types that should use webhook.

Routing rule:
- If event type is in `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES`, subscription uses Twitch webhook.
- Otherwise, subscription uses Twitch websocket.

Example:
- `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES=channel.online,channel.offline`
- `channel.online` and `channel.offline` go through webhook.
- `channel.chat.message` goes through websocket.

Twitch webhook callback:
- `POST /webhooks/twitch/eventsub`

Handler behavior:
- verifies `Twitch-Eventsub-Message-Signature` using HMAC-SHA256 over:
  `message_id + timestamp + raw_body`,
- handles `webhook_callback_verification` (returns raw challenge),
- handles `notification` and `revocation` with fast `2XX` responses.

## Production Deploy Over SSH
Linux/macOS:
```bash
./scripts/deploy.sh user@server /opt/twitch-eventsub-service
```

Windows PowerShell:
```powershell
./scripts/deploy.ps1 -RemoteHost user@server -RemotePath /opt/twitch-eventsub-service
```

Remote host requirements:
- Docker + Docker Compose plugin
- SSH access
- `.env` included in deployed project
