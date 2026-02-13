# Full Guide: Start API And Run Test App

This guide walks through a complete local run:
1. Start PostgreSQL.
2. Start the Twitch EventSub API.
3. Create a service account for API auth.
4. Start the Node test app.
5. Run an end-to-end event flow in the browser.

## 1) Prerequisites
- Python 3.11+
- Node.js 18+
- Docker Desktop or Podman Desktop (for local PostgreSQL)
- Twitch app credentials (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`)

## 2) Configure API Environment
From repo root:

```powershell
Copy-Item .env.example .env
```

Set at least these values in `.env`:
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`
- `TWITCH_REDIRECT_URI` (default local callback is `http://localhost:8080/oauth/callback`)
- `ADMIN_API_KEY`
- `SERVICE_SIGNING_SECRET`

Notes:
- Default DB URL in `.env.example` expects local Postgres on `localhost:5432`.
- For webhook-based upstream EventSub, also set:
  - `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL`
  - `TWITCH_EVENTSUB_WEBHOOK_SECRET`

## 3) Install API Dependencies
From repo root:

```powershell
pip install -e .
```

## 4) Start PostgreSQL
From repo root:

```powershell
docker compose up -d db
```

Verify DB container is running:

```powershell
docker compose ps
```

## 5) Start The API
From repo root:

```powershell
twitch-eventsub-api
```

Keep this terminal open.

Quick health check in another terminal:

```powershell
Invoke-RestMethod http://localhost:8080/health
```

## 6) Create Service Credentials For The Test App
In a new terminal, create a service account through admin API:

```powershell
$headers = @{ "X-Admin-Key" = "<ADMIN_API_KEY_FROM_.env>" }
$resp = Invoke-RestMethod -Method Post -Uri "http://localhost:8080/v1/admin/service-accounts?name=test-app" -Headers $headers
$resp
```

Save:
- `$resp.client_id`
- `$resp.client_secret`

They are needed by the test app.

## 7) Ensure You Have At Least One Bot Account
The test app needs a bot to create interests and send messages.

Open the CLI:

```powershell
twitch-eventsub-cli console
```

Use the bot setup flow in the console to add/authorize a bot account if none exists yet.

## 8) Configure Test App Environment
From repo root:

```powershell
Copy-Item test-app/.env.example test-app/.env
```

Set in `test-app/.env`:
- `SERVICE_BASE_URL=http://localhost:8080`
- `SERVICE_CLIENT_ID=<value from step 6>`
- `SERVICE_CLIENT_SECRET=<value from step 6>`
- optional: `TEST_WEBHOOK_PUBLIC_URL=<public url ending with /service-webhook>`

## 9) Install And Start Test App
From `test-app/`:

```powershell
npm install
npm start
```

Open:
- `http://localhost:9090`

## 10) End-To-End Browser Flow
In the test app UI:
1. Click refresh/status to verify service auth works.
2. Load/select an accessible bot.
3. Enter broadcaster ID (or resolve username to ID).
4. Optional but recommended for channel bot actions: start broadcaster grant and complete Twitch consent.
5. Connect service websocket.
6. Create interest (example: `channel.chat.message`, transport `websocket`).
7. Trigger an event (send a chat message or go live/offline depending on selected event type).
8. Confirm incoming envelope appears in live log.

Expected envelope fields include:
- `id`
- `provider` (`twitch`)
- `type`
- `event_timestamp`
- `event`

## 11) Optional: Webhook Delivery Test
1. Expose test app publicly (for example with ngrok).
2. Set `TEST_WEBHOOK_PUBLIC_URL` in `test-app/.env` to `<public-url>/service-webhook`.
3. Restart test app.
4. Create interest with `transport=webhook`.
5. Trigger event and verify `[webhook]` log entries in the test app UI.
6. If you observe webhook events that are no longer desired, delete matching webhook interests via `DELETE /v1/interests/{interest_id}`.

## 12) Stop Everything
API: `Ctrl+C` in API terminal.

Test app: `Ctrl+C` in test app terminal.

DB:

```powershell
docker compose down
```
