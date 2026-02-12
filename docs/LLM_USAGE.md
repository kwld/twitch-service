# LLM Integration Guide (Service Clients)

This guide explains how an LLM-powered client should use this API safely and completely.
It is written for agents that must perform real API work without guessing behavior.

## 1) Service Purpose
This service sits between your app and Twitch APIs/EventSub.
It provides:
- service-account authentication,
- managed EventSub subscriptions (deduplicated upstream),
- event fanout to your app (websocket or webhook),
- helper Twitch APIs (profiles, stream status),
- broadcaster authorization flow for bot-in-channel permissions,
- chat message sending via Twitch Helix (no `tmi.js`).

## 2) Identity Model
There are 3 identities involved:
1. Service account (your app identity in this service): `client_id`, `client_secret`.
2. Bot account (stored Twitch user token): `bot_account_id`.
3. Broadcaster authorization (channel grant for bot behavior in that channel): stored per service + bot + broadcaster.

Do not assume one bot can act in all channels without broadcaster authorization.

## 3) Authentication Rules
For service HTTP endpoints, always send headers:
- `X-Client-Id: <client_id>`
- `X-Client-Secret: <client_secret>`

For websocket event stream:
- `WS /ws/events?client_id=<client_id>&client_secret=<client_secret>`

Admin endpoints use `X-Admin-Key`, which service clients should not rely on.

## 4) Endpoint Map for Service Clients
Core:
- `GET /health`
- `GET /v1/eventsub/subscription-types`

Interest lifecycle:
- `GET /v1/interests`
- `POST /v1/interests`
- `DELETE /v1/interests/{interest_id}`
- `POST /v1/interests/{interest_id}/heartbeat`

Twitch helper reads:
- `GET /v1/twitch/profiles`
- `GET /v1/twitch/streams/status`
- `GET /v1/twitch/streams/status/interested`

Broadcaster auth:
- `POST /v1/broadcaster-authorizations/start`
- `GET /v1/broadcaster-authorizations`

Chat send:
- `POST /v1/twitch/chat/messages`

Event delivery:
- `WS /ws/events`
- outgoing webhook callbacks (if your interest transport is `webhook`)

## 5) Event Type Discovery (Always Do This First)
Use:
- `GET /v1/eventsub/subscription-types`

It returns:
- complete catalog snapshot,
- service-specific best transport recommendation per type,
- grouped lists: `webhook_preferred`, `websocket_preferred`.

LLM rule:
- Do not invent event type names.
- Validate requested type against catalog response before creating interests.

## 6) Interest Creation Contract
Request:
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
- `event_type` must exist in catalog.
- `transport=webhook` requires `webhook_url`.
- Duplicate-safe: same service+bot+event+broadcaster+transport+webhook_url reuses logical interest.

LLM behavior:
- Cache returned interest IDs.
- Heartbeat active interests periodically.

## 7) Event Delivery Modes
### Websocket mode
Connect:
- `WS /ws/events?client_id=...&client_secret=...`

Envelope shape:
```json
{
  "id": "message-id",
  "type": "channel.online",
  "event_timestamp": "2026-01-01T12:00:00+00:00",
  "event": {}
}
```

### Webhook mode
Set interest transport to webhook and provide `webhook_url`.
Service posts the same envelope JSON to your callback.

LLM behavior:
- Deduplicate by `id` if your downstream logic must be exactly-once.
- Implement reconnect/backoff for websocket clients.

## 8) Broadcaster Authorization Flow (Required for Bot-in-Channel Scenarios)
Start:
- `POST /v1/broadcaster-authorizations/start`
```json
{
  "bot_account_id": "uuid"
}
```

Response includes:
- `authorize_url`
- `state`
- `requested_scopes` (currently includes `channel:bot`)

Flow:
1. Your app redirects broadcaster to `authorize_url`.
2. Broadcaster approves on Twitch.
3. Service handles callback at `/oauth/callback`.
4. Service stores authorization mapping in DB.
5. Verify status with `GET /v1/broadcaster-authorizations`.

LLM behavior:
- Treat `state` as one-time correlation.
- Poll/list authorizations after redirect completion.
- If missing authorization for target channel, do not attempt badge/path-sensitive operations.

## 9) Send Chat Message API
Use:
- `POST /v1/twitch/chat/messages`

Request:
```json
{
  "bot_account_id": "uuid",
  "broadcaster_user_id": "12345",
  "message": "hello",
  "reply_parent_message_id": null,
  "auth_mode": "auto"
}
```

`auth_mode`:
- `auto`: try app-token send first, fallback to user-token send.
- `app`: app-token send only.
- `user`: user-token send only.

Response includes:
- `auth_mode_used`
- `bot_badge_eligible`
- `bot_badge_reason`
- message/drop info

Important:
- Broadcaster’s own channel for same user will not show bot badge.
- Badge-sensitive path depends on app-token mode + broadcaster channel grant (`channel:bot`) and Twitch-side conditions.

## 10) Twitch Scope Requirements (What LLMs Must Check)
Bot account token:
- `user:bot`
- `user:read:chat`
- `user:write:chat`

Broadcaster authorization:
- `channel:bot`

If missing scopes:
- instruct re-running guided OAuth for bot account,
- and/or re-running broadcaster authorization flow.

Reference docs:
- https://dev.twitch.tv/docs/chat/authenticating/
- https://dev.twitch.tv/docs/authentication/scopes/
- https://dev.twitch.tv/docs/api/reference/#send-chat-message

## 11) Recommended Full Lifecycle (LLM Playbook)
1. Validate service credentials once (`GET /v1/interests`).
2. Fetch catalog (`GET /v1/eventsub/subscription-types`).
3. Ensure bot account exists and is enabled (admin-provided out-of-band).
4. Ensure broadcaster authorization exists for target channel if needed.
5. Create interests.
6. Open websocket or receive webhook.
7. Heartbeat interests while active.
8. Send chat messages using `auth_mode=auto` unless forced.
9. On shutdown, delete interests.

## 12) Error Handling Strategy
Common statuses:
- `401`: invalid service credentials.
- `404`: resource not found (bot/interest).
- `409`: state conflict (disabled bot, missing required scopes for mode, token identity mismatch).
- `422`: invalid request (unsupported event type, missing webhook URL, malformed payload).
- `502`: upstream Twitch operation failure.

LLM behavior:
- Include actionable next step in error summaries.
- Never retry immediately on auth/scope errors.
- Retry with backoff for transient 5xx/network failures.

## 13) Idempotency and Safety Notes
- `POST /v1/interests` is duplicate-safe for identical tuples.
- Upstream events can still arrive with retries in edge cases; dedupe downstream by `id` if strict once-only effects are required.
- Keep local cache of active interests and authorization mappings to avoid redundant API calls.
