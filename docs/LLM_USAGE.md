# LLM-Friendly Service Usage Guide

This document is for automated agents that need to use this service.
It is intentionally strict and task-oriented.

## 1) Service Purpose
- Accept downstream service interests in Twitch EventSub events.
- Maintain deduplicated Twitch EventSub subscriptions.
- Receive Twitch events (websocket + webhook upstream).
- Fan out events to local services via:
  - downstream websocket (`/ws/events`)
  - downstream webhook callback URLs (per interest)

## 2) Required Configuration
Read from `.env`.

Minimum required keys:
- `APP_HOST`, `APP_PORT`
- `DATABASE_URL`
- `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
- `TWITCH_REDIRECT_URI`
- `TWITCH_EVENTSUB_WS_URL`
- `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL`
- `TWITCH_EVENTSUB_WEBHOOK_SECRET`
- `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES`
- `ADMIN_API_KEY`

Routing behavior:
- Event types listed in `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES` use upstream Twitch webhook transport.
- All other event types use upstream Twitch websocket transport.

## 3) Auth Model
### Admin auth
Use header:
- `X-Admin-Key: <ADMIN_API_KEY>`

### Service auth
Use headers:
- `X-Client-Id: <service_client_id>`
- `X-Client-Secret: <service_client_secret>`

### WS auth (downstream)
Use query params:
- `/ws/events?client_id=<id>&client_secret=<secret>`

## 4) Core Endpoints
### Health
- `GET /health`
- Response: `{"ok": true}`

### Admin: bots
- `GET /v1/bots`
- Auth: admin

### Admin: create service account
- `POST /v1/admin/service-accounts?name=<name>`
- Auth: admin
- Response includes one-time plaintext `client_secret`.

### Admin: list service accounts
- `GET /v1/admin/service-accounts`
- Auth: admin

### Admin: regenerate service secret
- `POST /v1/admin/service-accounts/{client_id}/regenerate`
- Auth: admin
- Response includes new one-time plaintext `client_secret`.

### Service: list interests
- `GET /v1/interests`
- Auth: service

### Service: create interest
- `POST /v1/interests`
- Auth: service
- JSON body:
```json
{
  "bot_account_id": "uuid",
  "event_type": "channel.online",
  "broadcaster_user_id": "12345",
  "transport": "websocket",
  "webhook_url": null
}
```
Rules:
- `transport` is downstream fanout transport, not Twitch upstream transport.
- If `transport = webhook`, `webhook_url` is required.

### Service: delete interest
- `DELETE /v1/interests/{interest_id}`
- Auth: service

### Downstream event stream websocket
- `WS /ws/events?client_id=...&client_secret=...`
- Receives JSON envelopes:
```json
{
  "id": "message-id",
  "type": "channel.online",
  "event_timestamp": "2026-01-01T12:00:00+00:00",
  "event": {}
}
```

### Twitch upstream webhook callback
- `POST /webhooks/twitch/eventsub`
- Validates Twitch signature HMAC.
- Handles:
  - `webhook_callback_verification`
  - `notification`
  - `revocation`

## 5) Recommended Agent Workflows
### A) Bootstrap a new downstream service
1. Call `POST /v1/admin/service-accounts?name=<name>`.
2. Persist returned `client_id` + `client_secret` securely.
3. Open WS connection to `/ws/events?...` or prepare downstream webhook endpoint.
4. Create interests with `POST /v1/interests`.

### B) Subscribe to online/offline + chat
1. Ensure `.env` contains:
   - `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES=channel.online,channel.offline`
2. Create interests for:
   - `channel.online`
   - `channel.offline`
   - chat event type(s), e.g. `channel.chat.message`
3. Service will route upstream automatically:
   - online/offline via Twitch webhook
   - chat via Twitch websocket

### C) Rotate service credentials
1. Call `POST /v1/admin/service-accounts/{client_id}/regenerate`.
2. Replace old secret everywhere.
3. Reconnect downstream websocket clients with new credentials.

## 6) Error Handling Expectations
- `401`: invalid admin key or service credentials.
- `404`: unknown bot/interest/service account.
- `422`: invalid request body (for example missing `webhook_url` with downstream webhook transport).
- `403` on `/webhooks/twitch/eventsub`: invalid Twitch signature or stale/invalid timestamp.

## 7) Idempotency and Dedup Semantics
- Multiple downstream services can register the same logical interest.
- Service deduplicates on Twitch side by `(bot_account_id, event_type, broadcaster_user_id)`.
- Removing one interest keeps Twitch subscription if others still depend on it.
- Removing last interest deletes Twitch subscription.

## 8) Operational Notes
- `.env` is mandatory. Service startup fails if missing.
- Startup behavior:
  - Load interests from DB.
  - Fetch Twitch subscriptions.
  - Reconcile DB state and ensure missing subscriptions.
- On Twitch websocket reconnect:
  - session is re-established
  - websocket-routed subscriptions are recreated for the new session as needed.
