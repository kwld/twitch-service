# External App Integration Guide

This guide covers only how an external application should consume this service.

## 1) What your app needs
- A service `client_id` and `client_secret` issued by this service.
- At least one `bot_account_id` to subscribe through.
- Base URL of the service (example: `http://localhost:8080`).

## 2) Authentication
For HTTP service endpoints, send:
- `X-Client-Id: <client_id>`
- `X-Client-Secret: <client_secret>`

For websocket event stream:
- `/ws/events?client_id=<client_id>&client_secret=<client_secret>`

## 3) Endpoints your app should use
### Health check
- `GET /health`

### List your interests
- `GET /v1/interests`

### Create interest
- `POST /v1/interests`
- Body:
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
- `transport` controls how this service sends events to your app.
- `transport=websocket`: you receive events on `/ws/events`.
- `transport=webhook`: you must provide `webhook_url`.

### Delete interest
- `DELETE /v1/interests/{interest_id}`

## 4) Receiving events
### Option A: websocket (recommended default)
Connect:
- `WS /ws/events?client_id=<id>&client_secret=<secret>`

Receive JSON envelope:
```json
{
  "id": "message-id",
  "type": "channel.online",
  "event_timestamp": "2026-01-01T12:00:00+00:00",
  "event": {}
}
```

### Option B: webhook callback in your app
When creating interest, set:
- `"transport": "webhook"`
- `"webhook_url": "https://your-app/callback"`

Your callback receives the same envelope JSON.

## 5) Typical flow for an external app
1. Open websocket connection to `/ws/events` (or prepare webhook endpoint).
2. Call `POST /v1/interests` for each event type/channel needed.
3. Store returned interest IDs.
4. On shutdown or unsubscribe, call `DELETE /v1/interests/{interest_id}`.
5. On reconnect, reopen websocket and keep existing interests.

## 6) Error handling
- `401`: invalid service credentials.
- `404`: interest not found (or bot not found on create).
- `422`: invalid request body (for example missing `webhook_url` for webhook transport).

## 7) Notes for client logic
- You may receive retries/duplicates from upstream sources in edge cases; dedupe by `id` if needed.
- Keep websocket reconnect logic in your app.
- Interest creation is not guaranteed idempotent by repeated identical POSTs; prefer storing and reusing created interest IDs.
