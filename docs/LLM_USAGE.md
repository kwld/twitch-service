# LLM Usage Contract (1:1 With Current Implementation)

This guide is intended for LLM agents integrating with the service API.
It describes current behavior from code, not aspirational behavior.

## 1) Required Headers
Service endpoints require both headers on every request:
- `X-Client-Id: <service_client_id>`
- `X-Client-Secret: <service_client_secret>`

Admin endpoints require:
- `X-Admin-Key: <admin_api_key>`

Service websocket (preferred):
- request short-lived token: `POST /v1/ws-token`
- connect: `WS /ws/events?ws_token=<token>`

Service websocket (legacy compatibility):
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

### Service user authentication (Twitch user login)
- `POST /v1/user-auth/start`
- `GET /v1/user-auth/session/{state}`

### Service websocket token
- `POST /v1/ws-token`

### Twitch helper reads
- `GET /v1/twitch/profiles`
- `GET /v1/twitch/streams/status`
- `GET /v1/twitch/streams/status/interested`
- `GET /v1/twitch/streams/live-test`
- `GET /v1/twitch/chat/assets`

### Chat send
- `POST /v1/twitch/chat/messages`

### Clip creation
- `POST /v1/twitch/clips`

### Event delivery
- `WS /ws/events`
- outgoing webhook callbacks to service-owned URLs.

## 5) How Other Apps Subscribe To Twitch Events
Use this order for a reliable integration:
1. Authenticate service calls with `X-Client-Id` + `X-Client-Secret`.
1. Discover available bots via `GET /v1/bots/accessible` and pick one allowed bot.
1. Fetch supported event types via `GET /v1/eventsub/subscription-types`.
1. If your event type needs broadcaster consent for the selected bot, run:
   - `POST /v1/broadcaster-authorizations/start`
   - complete Twitch OAuth redirect
   - confirm with `GET /v1/broadcaster-authorizations`.
1. Create subscription intent with `POST /v1/interests`:
   - choose downstream `transport`:
     - `websocket`: receive events over `WS /ws/events`
     - `webhook`: receive events via `webhook_url`
1. Keep interests alive with `POST /v1/interests/{interest_id}/heartbeat`.
1. Remove unused subscriptions with `DELETE /v1/interests/{interest_id}`.

Notes:
- `POST /v1/interests` deduplicates by service/bot/event/broadcaster/transport/webhook URL.
- Services choose only downstream delivery transport (`websocket` or `webhook`) from this bridge.
- Upstream Twitch transport is selected automatically by the bridge and is independent of downstream transport.
- Upstream policy:
  - if webhook callback is configured, bridge prefers Twitch webhook transport,
  - if webhook callback is not configured, bridge uses Twitch websocket transport fallback when supported,
  - webhook-only Twitch event types are never routed to upstream websocket.
- See `docs/EVENTSUB_TRANSPORT_CATALOG.md` for the transport-capability catalog used by this project.

## 6) Exact Request/Response Contracts

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
- `broadcaster_user_id` may be:
  - a numeric Twitch user id (preferred),
  - a Twitch login (e.g. `rajskikwiat`),
  - a Twitch channel URL (e.g. `https://www.twitch.tv/rajskikwiat`).
  The API resolves logins/URLs to a numeric user id before persisting.
- dedupe key:
  - `(service_account_id, bot_account_id, event_type, broadcaster_user_id, transport, webhook_url)`

Side effects:
- creates/upserts one logical interest.
- ensures upstream Twitch subscription exists.
- auto-ensures default stream interests for same `(service, bot, broadcaster)`:
  - `stream.online`
  - `stream.offline`

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
  "bot_account_id": "uuid",
  "redirect_url": "https://your-service.example.com/oauth/done"
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

`redirect_url` behavior:
- Optional; if present, callback completion redirects (HTTP `302`) to this URL.
- Redirect query fields on success:
  - `ok=true`
  - `message=Broadcaster authorization completed.`
  - `service_connected=true`
  - `broadcaster_user_id`
  - `broadcaster_login`
  - `scopes` (comma-separated)
- Redirect query fields on failure:
  - `ok=false`
  - `error`
  - `message`

### `GET /v1/broadcaster-authorizations`
Returns broadcaster grants for this service:
- includes `broadcaster_user_id`, `broadcaster_login`, `scopes`, timestamps.

### `POST /v1/user-auth/start`
Request:
```json
{
  "redirect_url": "https://your-service.example.com/twitch-auth/done"
}
```
Returns:
```json
{
  "state": "string",
  "authorize_url": "https://id.twitch.tv/oauth2/authorize?...",
  "requested_scopes": ["user:read:email"],
  "expires_in_seconds": 600
}
```

Behavior:
- creates a service-owned auth session keyed by `state`.
- authorize URL requests Twitch scope `user:read:email`.
- callback is completed by this service at `GET /oauth/callback`.

### `GET /v1/user-auth/session/{state}`
Returns service-owned auth session state:
- `status`: `pending|completed|failed`
- Twitch user identity fields: `twitch_user_id`, `twitch_login`, `twitch_display_name`, `twitch_email`
- OAuth token fields: `access_token`, `refresh_token`, `token_expires_at`
- metadata: `error`, `scopes`, `created_at`, `completed_at`

Ownership:
- `404` if state does not exist or belongs to another service account.

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

Optional query:
- `refresh` (bool, default `false`)
  - when `true`, the API refreshes stream state from Twitch Helix using the app token, updates `channel_states`, then returns the refreshed rows.

### `GET /v1/twitch/chat/assets`
Purpose:
- Fetch chat rendering assets (badges + emotes) for a broadcaster, to render incoming `channel.chat.*` notifications.

Query:
- `broadcaster` (required): numeric id, login, or Twitch channel URL.
- `refresh` (optional, default `false`): when `true`, force-refreshes the in-service cache from Twitch before returning.

Response:
- Includes `badges.global`, `badges.channel`, `emotes.global`, `emotes.channel` in the same shapes Twitch Helix returns.

Notes / Mapping rules (aligned with Twitch docs):
- EventSub chat payloads include:
  - `event.badges[]` entries with `set_id` + `id` (badge version id).
  - `event.message.fragments[]` entries of `type=emote` with `fragment.emote.id`.
- To render badges:
  - look up `set_id` + `id` in `badges.global.data[].versions[]` and `badges.channel.data[].versions[]`.
- To render emotes:
  - look up `fragment.emote.id` in `emotes.global.data[]` and `emotes.channel.data[]`.
- Webhook and WebSocket EventSub notifications use the same `subscription` and `event` objects (only metadata differs).

References (Twitch official docs):
- Helix API reference: chat badges.
  - `GET /helix/chat/badges/global` (Get Global Chat Badges): https://dev.twitch.tv/docs/api/reference#get-global-chat-badges
  - `GET /helix/chat/badges?broadcaster_id=...` (Get Channel Chat Badges): https://dev.twitch.tv/docs/api/reference#get-channel-chat-badges
- Helix API reference: emotes.
  - `GET /helix/chat/emotes/global` (Get Global Emotes): https://dev.twitch.tv/docs/api/reference#get-global-emotes
  - `GET /helix/chat/emotes?broadcaster_id=...` (Get Channel Emotes): https://dev.twitch.tv/docs/api/reference#get-channel-emotes
- EventSub reference: chat message structure (badges + message fragments/emotes).
  - `channel.chat.message`: https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/#channelchatmessage

### `GET /v1/twitch/streams/live-test`
Purpose:
- test if a single streamer is currently live for a connected service app.

Query:
- `bot_account_id` (required)
- one of:
  - `broadcaster_user_id`
  - `broadcaster_login`
- `refresh` (optional, default `true`)

Behavior:
- enforces service auth and bot-access policy.
- resolves `broadcaster_login` to Twitch user id when needed.
- with `refresh=true` (default):
  - fetches current state from Twitch,
  - upserts `channel_states`,
  - returns `source=\"twitch\"`.
- with `refresh=false`:
  - returns cached state only,
  - returns `404` if cache row does not exist,
  - returns `source=\"cache\"`.

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

### `POST /v1/twitch/clips`
Request:
```json
{
  "bot_account_id": "uuid",
  "broadcaster_user_id": "12345",
  "title": "Best moment",
  "duration": 30,
  "has_delay": false
}
```

Validation:
- `duration` must be between `5` and `60` seconds.
- bot must be accessible and enabled.
- bot token must include `clips:edit`.
- `has_delay=true` asks Twitch to clip from buffered video (slightly earlier than live edge), which is better for "clip what just happened" use cases.
- `has_delay=false` starts from the current live edge.

Multi-step behavior:
1. API calls Twitch Create Clip.
2. API polls Twitch Get Clips for up to 15 seconds.
3. response:
   - `status=ready` with clip URL metadata when Twitch finishes quickly,
   - `status=processing` with `clip_id` + `edit_url` if still processing.

LLM guidance:
- If user intent is "clip the moment that just happened", default to `has_delay=true`.
- If user intent is "start clipping from now", use `has_delay=false`.

## 7) OAuth Callback Semantics
Endpoint: `GET /oauth/callback`

Two behaviors share this endpoint:
1. Broadcaster grant completion:
   - when `state` matches pending broadcaster auth request.
2. CLI bot OAuth callback relay:
   - stores callback row in `oauth_callbacks` for CLI polling.
3. Service user-auth completion:
   - when `state` matches pending service user-auth request.

For broadcaster auth requests:
- exchanges code,
- validates granted scopes include `channel:bot`,
- resolves broadcaster identity from token validation,
- upserts `broadcaster_authorizations`,
- marks request completed/failed,
- if request includes `redirect_url`, returns HTTP `302` to that URL with result query params.

## 8) Event Delivery Semantics

### Service websocket (`WS /ws/events`)
- preferred auth: short-lived token from `POST /v1/ws-token`.
- legacy auth: direct query/header credentials (still accepted for compatibility).
- server accepts connection, tracks runtime stats.
- incoming client text messages are ignored (used as keepalive).
- server pushes event envelopes when matching interests fire.

### Service webhook delivery
- if interest transport is `webhook`, service posts envelope JSON to `webhook_url`.
- EventSub webhook callback requests are replay-protected by message-id dedupe (10 minute in-memory window).
- if your service receives a webhook event that it is no longer interested in, treat it as stale delivery and unsubscribe immediately by deleting matching interest rows.
- matching rule for unsubscribe:
  - compare incoming envelope `type` + `event.broadcaster_user_id` to your current desired subscriptions for the same bot/service context.
  - if not desired, call `GET /v1/interests`, find matching webhook interests, then call `DELETE /v1/interests/{interest_id}`.
- do not leave stale webhook interests active; they will continue to receive events until deleted.

Envelope shape emitted by local hub:
```json
{
  "id": "message-id",
  "provider": "twitch",
  "type": "event.type",
  "event_timestamp": "ISO8601",
  "event": {}
}
```

Subscription failure notification:
- when upstream Twitch subscription creation/rotation fails for an active interest key, the service emits a `subscription.error` envelope to every interested service using that service's configured transport (`websocket` or `webhook`).
- this includes permission failures (for example missing broadcaster authorization or missing scopes).
- notifications are rate-limited per `(service, bot, event_type, broadcaster, error_code)` for 1 minute to reduce spam.

`subscription.error` example:
```json
{
  "id": "generated-id",
  "provider": "twitch-service",
  "type": "subscription.error",
  "event_timestamp": "ISO8601",
  "event": {
    "error_code": "insufficient_permissions|missing_scope|unauthorized|subscription_create_failed",
    "reason": "raw upstream error text",
    "hint": "operator-friendly remediation hint",
    "event_type": "channel.chat.message",
    "broadcaster_user_id": "12345",
    "bot_account_id": "uuid",
    "upstream_transport": "websocket|webhook"
  }
}
```

LLM handling guideline for `subscription.error`:
1. Treat it as operational failure of upstream subscription state, not as a user-content event.
1. Parse `event.error_code` first, then route remediation:
   - `insufficient_permissions`: run broadcaster authorization flow for the same `(bot_account_id, broadcaster_user_id)`.
   - `missing_scope`: re-run bot OAuth setup to refresh scopes, then re-create interest.
   - `unauthorized`: verify selected bot is allowed for your service and still enabled; then retry with valid bot/auth.
   - `subscription_create_failed`: inspect `event.reason`, then retry with bounded backoff.
1. Do not hard-loop retries.
   - Use capped retry attempts with exponential backoff (for example 3 attempts over a few minutes).
1. Preserve subscription intent.
   - Keep or recreate the logical interest after remediation so manager can ensure upstream subscription again.
1. Surface operator diagnostics.
   - Log full envelope, include `bot_account_id`, `event_type`, `broadcaster_user_id`, `upstream_transport`, `reason`.
1. Escalate when persistent.
   - If the same key continues failing after remediation/retries, alert human operator and stop auto-retry for that key until manual action.

Optional enrichment (backward compatible):
- For `type` starting with `channel.chat.` the envelope may include:
  - `twitch_chat_assets`: best-effort lookup payload containing badge/emote image metadata referenced by the message.
  - Old clients should ignore unknown top-level keys.

## 9) Upstream Twitch EventSub Routing
Routing to Twitch transport is decided by manager using Twitch capability + runtime availability:
- webhook-only event types (from Twitch docs) always use upstream webhook.
- when webhook callback is configured, other types use upstream webhook (preferred).
- when webhook callback is unavailable, bridge uses upstream websocket fallback for types that support websocket.

This is independent of downstream service transport preference.
For bots, auth/token model for upstream subscription creation:
- Twitch webhook transport uses app access token.
- Twitch websocket transport uses bot user access token.

## 10) Error Model (Observed)
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

## 11) Strict LLM Playbook
1. Authenticate once with `GET /v1/interests`.
1. Fetch `GET /v1/bots/accessible`; choose only listed bot.
1. Fetch catalog `GET /v1/eventsub/subscription-types`.
1. If your product requires Twitch end-user login, run service user-auth (`/v1/user-auth/start` then poll `/v1/user-auth/session/{state}`).
1. If target channel requires channel grant, start and complete broadcaster auth.
1. Create interests.
1. If you need authoritative live status for interested channels, call `GET /v1/twitch/streams/status/interested?refresh=true` (or use `/v1/twitch/streams/live-test` for a single channel).
1. For websocket delivery, mint token via `POST /v1/ws-token`, then open `WS /ws/events?ws_token=...`.
1. Open webhook receiver if using webhook transport.
1. Heartbeat interests while active.
1. If you receive `subscription.error`, surface it to operators and run remediation (grant broadcaster authorization, refresh bot scopes, or switch bot).
1. On each incoming webhook, verify it is still desired; if not desired, delete matching webhook interests immediately.
1. Send chat with chosen `auth_mode`.
1. Create clips with `POST /v1/twitch/clips` when needed.
1. Delete interests when no longer needed.

## 12) Non-Service Endpoints (Admin/Operator)
For completeness:
- `GET /v1/bots` (admin)
- `POST /v1/admin/service-accounts` (admin)
- `GET /v1/admin/service-accounts` (admin)
- `POST /v1/admin/service-accounts/{client_id}/regenerate` (admin)
- CLI console provides additional management operations not exposed as service APIs.
