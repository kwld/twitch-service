# Twitch EventSub Service

Minimal API service that:
- manages Twitch bot OAuth credentials in PostgreSQL,
- maintains deduplicated Twitch EventSub subscriptions,
- receives Twitch EventSub over WebSocket,
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
