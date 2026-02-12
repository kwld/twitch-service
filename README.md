# Twitch EventSub Service

Minimal API service that:
- manages Twitch bot OAuth credentials in PostgreSQL,
- maintains deduplicated Twitch EventSub subscriptions,
- receives Twitch EventSub over WebSocket or Webhook,
- forwards events to local services over WebSocket or outgoing webhooks.

## Documentation Map
- `README.md`: operator-facing overview, setup, deploy, and feature map.
- `docs/ARCHITECTURE.md`: runtime internals and component behavior.
- `docs/LLM_USAGE.md`: strict integration contract for LLM/service clients (code-aligned).
- `docs/DEV_SETUP.md`: local development container workflow.
- `docs/PRODUCTION_DEPLOY.md`: production deployment procedure.

## Features
- Async interactive CLI (`twitch-eventsub-cli console`) to:
  - guided Twitch bot setup wizard (OAuth),
  - add/list/refresh bot accounts,
  - manage service accounts (`client_id`, `client_secret`) in a submenu:
    - create account,
    - regenerate secret,
    - delete account,
    - grant/revoke bot access per service.
- API for local services to register interest subscriptions.
- API to list EventSub subscription catalog and transport recommendations.
- API for services to list effective bot access (`GET /v1/bots/accessible`).
- API for local services to fetch Twitch user profiles and stream status via bot accounts.
- In-memory + database interest registry.
- Startup reconciliation:
  - load interests from DB,
  - fetch existing Twitch subscriptions,
  - reuse existing ones when possible,
  - create only missing subscriptions.
- On startup, initializes stream state for interested channels.
- Reconnect support for Twitch EventSub WebSocket.
- Twitch webhook callback verification + HMAC signature validation.
- Auto-prunes stale interests: if not heartbeated for 1 hour, interest/state is removed.

For exact runtime flow and data model details, see `docs/ARCHITECTURE.md`.
For 1:1 LLM integration behavior and endpoint contracts, see `docs/LLM_USAGE.md`.

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
- `GET /oauth/callback` (OAuth redirect handler for bot setup + broadcaster channel authorization)
- `POST /webhooks/twitch/eventsub` (Twitch webhook callback)
- `GET /v1/bots` (admin)
- `POST /v1/admin/service-accounts?name=<name>` (admin)
- `GET /v1/admin/service-accounts` (admin)
- `POST /v1/admin/service-accounts/{client_id}/regenerate` (admin)
- `GET /v1/interests` (service)
- `GET /v1/bots/accessible` (service)
- `POST /v1/broadcaster-authorizations/start` (service)
- `GET /v1/broadcaster-authorizations` (service)
- `GET /v1/eventsub/subscription-types` (service)
- `POST /v1/interests` (service)
- `DELETE /v1/interests/{interest_id}` (service)
- `POST /v1/interests/{interest_id}/heartbeat` (service)
- `GET /v1/twitch/profiles?bot_account_id=...&user_ids=...&logins=...` (service)
- `GET /v1/twitch/streams/status?bot_account_id=...&broadcaster_user_ids=...` (service)
- `GET /v1/twitch/streams/status/interested` (service)
- `POST /v1/twitch/chat/messages` (service)
- `WS /ws/events?client_id=...&client_secret=...` (service)

### Service Bot Access Policy
- Services can be restricted to a subset of bots.
- If a service has no explicit bot-access mappings, it can access all enabled bots (default mode).
- If mappings exist, access is restricted to only mapped bots.
- Service can inspect effective access via `GET /v1/bots/accessible`.
- Bot-specific service endpoints return `403` when service is not allowed to access the requested bot.

### Service Secret Hashing
- Service account secrets are hashed with PBKDF2-SHA256 (`pbkdf2_sha256$...`) to avoid bcrypt backend/runtime issues and length limits.
- Legacy bcrypt hashes remain verifiable for backward compatibility.

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

`POST /v1/interests` also:
- validates `event_type` against the known Twitch EventSub catalog,
- deduplicates per-service interests (same service + bot + event_type + broadcaster + transport + webhook_url).

Interests should be heartbeated periodically by client services:
- call `POST /v1/interests/{interest_id}/heartbeat`
- if no heartbeat for 1 hour, service auto-removes stale interests and channel state.

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
- `scripts/cli-container.ps1`
- `scripts/cli-container.sh`

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

### Open CLI from project root in container
Windows PowerShell:
```powershell
./scripts/cli-container.ps1 -Engine docker
```
or
```powershell
./scripts/cli-container.ps1 -Engine podman
```

Linux/macOS shell:
```bash
./scripts/cli-container.sh docker
```
or
```bash
./scripts/cli-container.sh podman
```

If the dev app container is running, scripts attach via `exec`.
If not running, scripts start one-off `compose run` CLI container.

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

`user.authorization.revoke` is always managed as webhook subscription and disables matching bot account when received.

Catalog source for `GET /v1/eventsub/subscription-types`:
- https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/

### Send Chat Message Payload
```json
{
  "bot_account_id": "uuid",
  "broadcaster_user_id": "12345",
  "message": "hello chat",
  "reply_parent_message_id": null,
  "auth_mode": "auto"
}
```

`POST /v1/twitch/chat/messages` uses Twitch Helix `chat/messages`.
- `auth_mode=auto` (default): try app-token send first (bot-badge path), then fallback to user token.
- `auth_mode=app`: app-token send only.
- `auth_mode=user`: user-token send only.
Required bot scope: `user:write:chat`.
Required broadcaster channel authorization scope: `channel:bot`.

## Streamer Authorization Flow (Bot In Streamer Channel)
Yes, the required redirect API is implemented. Broadcaster authorization is handled by:
- `POST /v1/broadcaster-authorizations/start` (service starts flow and gets Twitch `authorize_url`)
- `GET /oauth/callback` (Twitch redirects here after streamer consent)
- `GET /v1/broadcaster-authorizations` (service checks stored authorizations)

Use this flow for each streamer channel where the bot should act as a cloud bot:
1. Ensure bot account OAuth token includes: `user:read:chat`, `user:write:chat`, `user:bot`.
2. Service calls `POST /v1/broadcaster-authorizations/start` with `bot_account_id`.
   - Optional: include `redirect_url` so callback redirects back to your app after consent.
3. Redirect streamer in browser to returned `authorize_url`.
4. Streamer approves Twitch consent for scope `channel:bot`.
5. Twitch redirects to this service at `TWITCH_REDIRECT_URI` (this app handles `/oauth/callback`).
6. Service verifies with `GET /v1/broadcaster-authorizations` before creating chat subscriptions or sending bot-badge-eligible messages.

If `redirect_url` is provided, `/oauth/callback` responds with HTTP `302` to that URL and appends query fields:
- success: `ok=true`, `message`, `service_connected=true`, `broadcaster_user_id`, `broadcaster_login`, `scopes` (comma-separated),
- failure: `ok=false`, `error`, `message`.

If streamer authorization is missing, Twitch may return:
- `403 subscription missing proper authorization`

If bot scopes are missing, chat/eventsub actions may fail until bot OAuth is refreshed.

Twitch references:
- https://dev.twitch.tv/docs/chat/authenticating/
- https://dev.twitch.tv/docs/authentication/scopes/
- https://dev.twitch.tv/docs/api/reference/#create-eventsub-subscription
- https://dev.twitch.tv/docs/api/reference/#send-chat-message

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

## Production Deploy From Fresh Clone (Local Host)
Use these scripts when running directly on a server/host after cloning.

Windows PowerShell:
```powershell
./scripts/prod-container.ps1 -Engine docker -Build
```
or
```powershell
./scripts/prod-container.ps1 -Engine podman -Build
```

Linux/macOS shell:
```bash
./scripts/prod-container.sh docker --build
```
or
```bash
./scripts/prod-container.sh podman --build
```

Behavior:
- bootstraps `.env` from `.env.example` if missing,
- sets `APP_ENV=prod`,
- enforces container-safe DB URL (`db:5432`),
- auto-generates secure secrets when placeholders are present,
- starts `docker-compose.yml` with `up -d --remove-orphans`.

After first start, update `.env` with real Twitch values:
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`
- `TWITCH_REDIRECT_URI`
- `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL`

Then restart compose.

Full production guide: `docs/PRODUCTION_DEPLOY.md`.

## Node Frontend Test App
A browser-based test app (Node backend + static frontend) is available in `test-app/`.

It covers:
- service auth + accessible bot discovery (`/v1/bots/accessible`),
- broadcaster authorization bootstrap,
- interest lifecycle (create/list/heartbeat/delete),
- websocket event listening and live UI log,
- optional webhook receive transport via `/service-webhook`,
- chat send via service endpoint.

See `test-app/README.md`.
# twitch-service
