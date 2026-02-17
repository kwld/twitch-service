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
- `docs/EVENTSUB_TRANSPORT_CATALOG.md`: Twitch upstream transport capability snapshot used by this project.
- `docs/DEV_SETUP.md`: local development container workflow.
- `docs/START_AND_TEST_APP.md`: full step-by-step local walkthrough (start API + run test app).
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
  - live per-service communication tracking (incoming from Twitch and outgoing to service transports) with redacted payloads/secrets.
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

## Service User Authentication Flow
Service clients can authenticate Twitch end-users with OAuth scope `user:read:email`.

Endpoints:
- `POST /v1/user-auth/start`
- `GET /v1/user-auth/session/{state}`
- callback is handled by `GET /oauth/callback`

Flow:
1. Service calls `POST /v1/user-auth/start` (optional `redirect_url`).
2. Service redirects user to returned `authorize_url`.
3. Twitch redirects to service callback (`/oauth/callback`) after consent.
4. Service polls `GET /v1/user-auth/session/{state}` until status is `completed` or `failed`.
5. On `completed`, session includes authenticated Twitch identity, email, and OAuth tokens.

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

## Helper Scripts (Install, Run, Run Dev)
These scripts validate `.env` and guide setup when file or required properties are missing.

PowerShell:
```powershell
./scripts/install.ps1
./scripts/run.ps1
./scripts/run-dev.ps1 -Port 8080
```

Bash:
```bash
bash ./scripts/install.sh
bash ./scripts/run.sh
bash ./scripts/run-dev.sh 8080
```

## API Authentication
- Admin endpoints use header: `X-Admin-Key: <ADMIN_API_KEY>`
- Service endpoints use:
  - `X-Client-Id: <client_id>`
  - `X-Client-Secret: <client_secret>`

## IP Allowlist
- `APP_ALLOWED_IPS`: comma-separated IPv4/IPv6 addresses or CIDRs allowed to access the service.
- `APP_TRUST_X_FORWARDED_FOR`: set `true` only when running behind a trusted reverse proxy; then first `X-Forwarded-For` IP is used.
- Empty `APP_ALLOWED_IPS` means no IP restriction.
- The general allowlist does not block `POST /webhooks/twitch/eventsub`; that endpoint remains protected by Twitch HMAC signature verification.

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
- `POST /v1/user-auth/start` (service)
- `GET /v1/user-auth/session/{state}` (service)
- `POST /v1/ws-token` (service)
- `GET /v1/eventsub/subscription-types` (service)
- `POST /v1/interests` (service)
- `DELETE /v1/interests/{interest_id}` (service)
- `POST /v1/interests/{interest_id}/heartbeat` (service)
- `GET /v1/subscriptions` (service)
- `GET /v1/subscriptions/transports` (service)
- `GET /v1/twitch/profiles?bot_account_id=...&user_ids=...&logins=...` (service)
- `GET /v1/twitch/streams/status?bot_account_id=...&broadcaster_user_ids=...` (service)
- `GET /v1/twitch/streams/status/interested` (service)
- `GET /v1/twitch/streams/status/interested?refresh=true` (service)
- `GET /v1/twitch/streams/live-test?bot_account_id=...&broadcaster_user_id=...` (service)
- `GET /v1/twitch/chat/assets?broadcaster=...&refresh=false` (service)
- `POST /v1/twitch/chat/messages` (service)
- `POST /v1/twitch/clips` (service)
- `WS /ws/events?ws_token=...` (service)

Note on live status:
- `GET /v1/twitch/streams/status/interested` returns cached `ChannelState`.
- Use `GET /v1/twitch/streams/status/interested?refresh=true` to force-refresh from Twitch Helix.

### Service Event Envelope
Events delivered via service websocket (`/ws/events`) and service webhooks include:
```json
{
  "id": "message-id",
  "provider": "twitch",
  "type": "event.type",
  "event_timestamp": "ISO8601",
  "event": {}
}
```

Optional enrichment (backward compatible):
- For `type` starting with `channel.chat.` the service may include `twitch_chat_assets` in the envelope.
- Old clients should ignore unknown top-level keys.

### Service Bot Access Policy
- Services can be restricted to a subset of bots.
- If a service has no explicit bot-access mappings, it can access all enabled bots (default mode).
- If mappings exist, access is restricted to only mapped bots.
- Service can inspect effective access via `GET /v1/bots/accessible`.
- Bot-specific service endpoints return `403` when service is not allowed to access the requested bot.

### Service Secret Hashing
- Service account secrets are hashed with PBKDF2-SHA256 (`pbkdf2_sha256$...`) to avoid bcrypt backend/runtime issues and length limits.
- Legacy bcrypt hashes remain verifiable for backward compatibility.

### Service WebSocket Auth
- Preferred flow:
  1. call `POST /v1/ws-token` with service headers,
  2. connect `WS /ws/events?ws_token=<token>`.
- WS tokens are short-lived and single-use.
- Backward compatibility: `client_id` + `client_secret` are still accepted on websocket query/header auth, but should be treated as legacy.

### Create Interest Payload
```json
{
  "bot_account_id": "uuid",
  "event_type": "stream.online",
  "broadcaster_user_id": "12345",
  "transport": "websocket",
  "webhook_url": null
}
```

For `transport=webhook`, `webhook_url` is required.

`POST /v1/interests` also:
- validates `event_type` against the known Twitch EventSub catalog,
- deduplicates per-service interests (same service + bot + event_type + broadcaster + transport + webhook_url).

`broadcaster_user_id` input:
- preferred: numeric Twitch user id (string).
- also accepted: Twitch login (e.g. `rajskikwiat`) or a Twitch channel URL (e.g. `https://www.twitch.tv/rajskikwiat`).
  - the API resolves logins/URLs to numeric user ids before persisting.
  - best-effort migration: if older interests/channel state rows stored a login/URL, the API will migrate them to the resolved numeric id.

Interests should be heartbeated periodically by client services:
- call `POST /v1/interests/{interest_id}/heartbeat`
- if no heartbeat for 1 hour, service auto-removes stale interests and channel state.

Webhook consumer rule:
- if your service receives webhook events it no longer wants, unsubscribe immediately.
- do this by listing current interests (`GET /v1/interests`) and deleting matching webhook interests (`DELETE /v1/interests/{interest_id}`).
- keeping stale webhook interests active will continue event delivery.

## Container CLI Helper
Kept helper scripts:
- `scripts/cli-container.ps1`
- `scripts/cli-container.sh`

Use these from project root to run `twitch-eventsub-cli` inside the dev app container.

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

## Upstream EventSub Routing (Automatic)
The service chooses upstream Twitch transport automatically per event type.

Configure in `.env`:
- `TWITCH_EVENTSUB_WS_URL`: websocket endpoint.
- `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL`: public HTTPS callback.
- `TWITCH_EVENTSUB_WEBHOOK_SECRET`: webhook secret (10-100 ASCII chars).

Routing rule:
- webhook-only Twitch event types are always webhook upstream.
- if webhook callback config is available, webhook is preferred upstream for supported types.
- if webhook callback config is unavailable, websocket is used as fallback for types that support websocket.

See `docs/EVENTSUB_TRANSPORT_CATALOG.md` for the current capability snapshot and webhook-only exceptions.

Twitch webhook callback:
- `POST /webhooks/twitch/eventsub`

`user.authorization.revoke` is always managed as webhook subscription and disables matching bot account when received.

Catalog source for `GET /v1/eventsub/subscription-types`:
- https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/

## Loki EventSub Logging (Optional)
The service writes structured, redacted EventSub audit logs to:
- `APP_EVENTSUB_LOG_PATH` (default `./logs/eventsub.log`)

Optional env vars:
- `LOKI_HOST`
- `LOKI_PORT`

When both `LOKI_HOST` and `LOKI_PORT` are defined in `.env`, helper scripts start Grafana Alloy and Alloy pushes `eventsub.log` to Loki (`http://LOKI_HOST:LOKI_PORT/loki/api/v1/push`).
If either is missing, Alloy is skipped.

Redaction:
- sensitive fields (tokens/secrets/auth/api keys/passwords) are masked as `***` + last 4 characters.

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

### Create Clip Payload
```json
{
  "bot_account_id": "uuid",
  "broadcaster_user_id": "12345",
  "title": "Best moment",
  "duration": 30,
  "has_delay": false
}
```

`POST /v1/twitch/clips` is a multi-step helper:
1. Calls Twitch Create Clip.
2. Polls Twitch Get Clips for up to 15 seconds until clip metadata is available.
3. Returns `status=ready` with URLs, or `status=processing` if Twitch is still preparing the clip.

Required bot scope: `clips:edit`.
Duration must be between 5 and 60 seconds.
`has_delay=true` tells Twitch to use buffered video (slightly earlier than live edge), useful for clipping a moment that just happened.
`has_delay=false` starts clip capture from the current live edge.

## Streamer Authorization Flow (Bot In Streamer Channel)
Yes, the required redirect API is implemented. Broadcaster authorization is handled by:
- `POST /v1/broadcaster-authorizations/start` (service starts flow and gets Twitch `authorize_url`)
- `GET /oauth/callback` (Twitch redirects here after streamer consent)
- `GET /v1/broadcaster-authorizations` (service checks stored authorizations)

Use this flow for each streamer channel where the bot should act as a cloud bot:
1. Ensure bot account OAuth token includes: `user:read:chat`, `user:write:chat`, `user:bot`, `clips:edit`.
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
- deduplicates webhook `message_id` values in-memory (10 minute window) to reject replayed notifications/challenges/revocations,
- handles `webhook_callback_verification` (returns raw challenge),
- handles `notification` and `revocation` with fast `2XX` responses.

## Production Deploy
Production deployment guidance remains in `docs/PRODUCTION_DEPLOY.md`.

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
