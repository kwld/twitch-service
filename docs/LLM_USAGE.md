# LLM Usage Contract (1:1 With Current Implementation)

This guide is intended for LLM agents integrating with the service API.
It describes current behavior from code, not aspirational behavior.

## 1) Required Headers
Service endpoints require both headers on every request:
- `X-Client-Id: <service_client_id>`
- `X-Client-Secret: <service_client_secret>`

Admin endpoints require:
- `X-Admin-Key: <admin_api_key>`

Service websocket requires query params:
- `WS /ws/events?client_id=<id>&client_secret=<secret>`

## 2) Identity Model
There are 3 identity layers:
1. Service account:
   - API client identity for your app/integration.
2. Bot account:
   - Twitch user identity with stored OAuth user token.
3. Broadcaster authorization:
   - per `(service, bot, broadcaster)` grant that enables channel-level bot behavior.

Never assume one bot can operate in all channels without broadcaster authorization.

## 3) Access Control For Bots
Service-to-bot access policy:
- if service has no explicit mappings in `service_bot_access`: access mode is `all`.
- if mappings exist: access mode is `restricted` to mapped bots only.

Use:
- `GET /v1/bots/accessible`

Response:
```json
{
  "access_mode": "all|restricted",
  "bots": [
    {
      "id": "uuid",
      "name": "string",
      "twitch_user_id": "string",
      "twitch_login": "string",
      "enabled": true
    }
  ]
}
```

LLM rule:
- Before any bot-scoped call, fetch `/v1/bots/accessible`.
- Reject or re-route requests that reference bots not listed.

## 4) Endpoint Catalog (Service Side)

### Core
- `GET /health`
- `GET /v1/eventsub/subscription-types`
- `GET /v1/bots/accessible`

### Interest lifecycle
- `GET /v1/interests`
- `POST /v1/interests`
- `DELETE /v1/interests/{interest_id}`
- `POST /v1/interests/{interest_id}/heartbeat`

### Broadcaster authorization
- `POST /v1/broadcaster-authorizations/start`
- `GET /v1/broadcaster-authorizations`

### Twitch helper reads
- `GET /v1/twitch/profiles`
- `GET /v1/twitch/streams/status`
- `GET /v1/twitch/streams/status/interested`

### Chat send
- `POST /v1/twitch/chat/messages`

### Event delivery
- `WS /ws/events`
- outgoing webhook callbacks to service-owned URLs.

## 5) Exact Request/Response Contracts

### `GET /v1/interests`
Returns list of interest rows owned by authenticated service.

### `POST /v1/interests`
Request:
```json
{
  "bot_account_id": "uuid",
  "event_type": "channel.chat.message",
  "broadcaster_user_id": "12345",
  "transport": "websocket|webhook",
  "webhook_url": "https://..." 
}
```

Validation behavior:
- `event_type` must exist in known catalog.
- `transport=webhook` requires `webhook_url`.
- bot must exist and be accessible by service policy.
- dedupe key:
  - `(service_account_id, bot_account_id, event_type, broadcaster_user_id, transport, webhook_url)`

Side effects:
- creates/upserts one logical interest.
- ensures upstream Twitch subscription exists.
- auto-ensures default stream interests for same `(service, bot, broadcaster)`:
  - `channel.online`
  - `channel.offline`

### `DELETE /v1/interests/{interest_id}`
- deletes interest only if owned by service.
- if no remaining interest uses the key, upstream Twitch subscription is removed.
- may remove channel state row for that bot+broadcaster key.

### `POST /v1/interests/{interest_id}/heartbeat`
- touches `updated_at` on all interests for same `(service, bot, broadcaster)` as target interest.
- stale interests are auto-pruned by manager after 1 hour inactivity.

### `GET /v1/eventsub/subscription-types`
Returns:
- source URL/date snapshot,
- grouped `webhook_preferred` and `websocket_preferred`,
- complete `all_items`.

LLM rule:
- never invent event types; choose from returned catalog.

### `POST /v1/broadcaster-authorizations/start`
Request:
```json
{
  "bot_account_id": "uuid"
}
```
Returns:
```json
{
  "state": "string",
  "authorize_url": "https://id.twitch.tv/oauth2/authorize?...",
  "requested_scopes": ["channel:bot"],
  "expires_in_seconds": 600
}
```

### `GET /v1/broadcaster-authorizations`
Returns broadcaster grants for this service:
- includes `broadcaster_user_id`, `broadcaster_login`, `scopes`, timestamps.

### `GET /v1/twitch/profiles`
Query:
- `bot_account_id` (required)
- `user_ids` and/or `logins` (CSV)
Rules:
- at least one of `user_ids`/`logins`,
- max combined 100 values.

### `GET /v1/twitch/streams/status`
Query:
- `bot_account_id`
- `broadcaster_user_ids` (CSV, required, max 100)

Behavior:
- fetches live stream states from Twitch,
- updates/creates cached `channel_states`,
- returns rows for requested broadcasters.

### `GET /v1/twitch/streams/status/interested`
Returns cached stream rows for pairs derived from current service interests.

### `POST /v1/twitch/chat/messages`
Request:
```json
{
  "bot_account_id": "uuid",
  "broadcaster_user_id": "12345",
  "message": "hello",
  "reply_parent_message_id": null,
  "auth_mode": "auto|app|user"
}
```

Behavior:
- verifies bot access policy and bot enabled state.
- validates bot token ownership (`token.user_id == bot.twitch_user_id`).
- scope checks:
  - always `user:write:chat`
  - for `auto`/`app`: also `user:bot`
- send mode:
  - `auto`: app-token send first, fallback to user-token.
  - `app`: app-token only.
  - `user`: user-token only.

Response fields include:
- `is_sent`, `message_id`,
- `auth_mode_used`,
- `bot_badge_eligible`, `bot_badge_reason`,
- `drop_reason_code`, `drop_reason_message`.

## 6) OAuth Callback Semantics
Endpoint: `GET /oauth/callback`

Two behaviors share this endpoint:
1. Broadcaster grant completion:
   - when `state` matches pending broadcaster auth request.
2. CLI bot OAuth callback relay:
   - stores callback row in `oauth_callbacks` for CLI polling.

For broadcaster auth requests:
- exchanges code,
- validates granted scopes include `channel:bot`,
- resolves broadcaster identity from token validation,
- upserts `broadcaster_authorizations`,
- marks request completed/failed.

## 7) Event Delivery Semantics

### Service websocket (`WS /ws/events`)
- authenticate with query credentials.
- server accepts connection, tracks runtime stats.
- incoming client text messages are ignored (used as keepalive).
- server pushes event envelopes when matching interests fire.

### Service webhook delivery
- if interest transport is `webhook`, service posts envelope JSON to `webhook_url`.

Envelope shape emitted by local hub:
```json
{
  "id": "message-id",
  "type": "event.type",
  "event_timestamp": "ISO8601",
  "event": {}
}
```

## 8) Upstream Twitch EventSub Routing
Routing to Twitch transport is decided by manager:
- `user.authorization.revoke` => webhook only.
- event type in `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES` => webhook.
- all others => websocket.

This is independent of downstream service transport preference.

## 9) Error Model (Observed)
- `401`: invalid service credentials.
- `403`: authenticated service not authorized for selected bot.
- `404`: missing bot/interest/service resource.
- `409`:
  - bot disabled,
  - required scope missing,
  - token identity mismatch.
- `422`:
  - invalid event type,
  - missing required webhook URL,
  - malformed query constraints.
- `502`: upstream Twitch failures (OAuth, EventSub, chat send, Helix calls).

LLM rule:
- For auth/scope/authorization failures, do not blind-retry.
- Return actionable remediation:
  - refresh bot OAuth scopes,
  - run broadcaster grant,
  - choose accessible bot,
  - validate credentials.

## 10) Strict LLM Playbook
1. Authenticate once with `GET /v1/interests`.
2. Fetch `GET /v1/bots/accessible`; choose only listed bot.
3. Fetch catalog `GET /v1/eventsub/subscription-types`.
4. If target channel requires channel grant, start and complete broadcaster auth.
5. Create interests.
6. Open websocket and/or webhook receiver.
7. Heartbeat interests while active.
8. Send chat with chosen `auth_mode`.
9. Delete interests when no longer needed.

## 11) Non-Service Endpoints (Admin/Operator)
For completeness:
- `GET /v1/bots` (admin)
- `POST /v1/admin/service-accounts` (admin)
- `GET /v1/admin/service-accounts` (admin)
- `POST /v1/admin/service-accounts/{client_id}/regenerate` (admin)
- CLI console provides additional management operations not exposed as service APIs.
