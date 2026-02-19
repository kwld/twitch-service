# Architecture And Runtime Behavior

This document describes how the application works from process startup to event delivery.

## 1) Core Components
- `app/main.py`: FastAPI app, API endpoints, auth dependencies, lifecycle startup/shutdown.
- `app/core/`: shared security/runtime primitives (`network_security`, `runtime_tokens`, `redaction`, `normalization`).
- `app/eventsub_manager.py`: reconciles and maintains upstream Twitch EventSub subscriptions.
- `app/eventsub_manager_parts/subscription_mixin.py`: EventSub reconcile/ensure/rotation logic and subscription error signaling.
- `app/eventsub_manager_parts/notification_mixin.py`: incoming notification handling, fanout delivery, audit/trace logging, and stream-state updates.
- `app/event_router.py`:
  - `InterestRegistry`: in-memory map of service interests.
  - `LocalEventHub`: local fanout over websocket and outgoing webhooks.
- `app/twitch.py`: Twitch OAuth/Helix client wrapper.
- `app/cli.py`: async operator console (bot setup, service account management, live chat tools).
- `app/cli_components/remote_console.py`: remote API console workflows used by `app/cli.py`.
- `app/cli_components/monitoring.py`: service status, broadcaster authorization listing, tracked-channel views, and live event-trace tracking for CLI.
- `app/cli_components/service_management.py`: CLI workflows for service-account CRUD, service bot access, and bot self-channel authorization.
- `app/cli_components/bot_workflows.py`: bot OAuth callback polling, guided bot setup, and enabled-bot selection helpers.
- `app/cli_components/chat_tools.py`: CLI live chat, clip creation, and bot removal workflows.
- `app/cli_components/eventsub_tools.py`: CLI EventSub subscription listing/removal workflows.
- `app/cli_components/interactive_tools.py`: thin compatibility re-export for chat/eventsub tool entrypoints.
- `app/routes/system_routes.py`: system route wiring (`/health`) and registration of OAuth/webhook route modules.
- `app/routes/oauth_routes.py`: OAuth callback endpoint logic (`/oauth/callback`) for broadcaster and service-user auth flows.
- `app/routes/webhook_routes.py`: Twitch EventSub webhook callback endpoint logic (`/webhooks/twitch/eventsub`).
- `app/routes/ws_routes.py`: websocket auth endpoint and websocket mismatch handlers (`/ws/events`, `/socket.io`, catch-all mismatch).
- `app/models.py`: SQLAlchemy schema and relationships.
- `app/auth.py`: service credential generation and verification.

## 2) Startup Sequence
At app startup (`lifespan` in `app/main.py`):
1. Create DB tables via `Base.metadata.create_all`.
2. `EventSubManager.start()`:
   - load persisted interests from DB into `InterestRegistry`,
   - fetch current Twitch EventSub subscriptions and reconcile local DB subscription rows,
   - ensure `user.authorization.revoke` webhook subscription exists,
   - ensure configured webhook-routed event subscriptions exist,
   - refresh stream states for channels represented by active `stream.online`/`stream.offline` EventSub subscriptions (Helix confirmation),
   - refresh stream states for channels currently represented by interests.
3. Start EventSub manager loop tasks:
   - websocket manager loop (upstream Twitch EventSub),
   - stale-interest cleanup loop (every 5 minutes; removes interests older than 1 hour).

At shutdown:
- stop manager tasks,
- dispose SQLAlchemy engine.

## 3) Data Model
Main tables:
- `bot_accounts`: Twitch bot identities and OAuth tokens.
- `service_accounts`: service client credentials (`client_id`, hashed `client_secret`).
- `service_bot_access`: optional per-service bot allow-list.
  - no rows for a service => access to all enabled bots.
  - one or more rows => restricted to those bot IDs.
- `service_interests`: logical subscriptions requested by services.
- `twitch_subscriptions`: upstream Twitch EventSub subscription records.
- `channel_states`: cached live/offline state for `(bot, broadcaster)`.
- `broadcaster_authorization_requests`: pending/completed OAuth grant attempts for broadcaster channel access.
- `broadcaster_authorizations`: completed grants (service + bot + broadcaster).
- `service_user_auth_requests`: pending/completed Twitch end-user auth sessions per service (`user:read:email`).
- `oauth_callbacks`: callback relay storage for CLI OAuth polling.
- `service_runtime_stats`: per-service counters and connection/event timestamps.
- `service_event_traces`: redacted incoming/outgoing communication traces per service for operator live tracking.

## 4) Authentication Model
- Admin endpoints: `X-Admin-Key` must match `ADMIN_API_KEY`.
- Service endpoints:
  - `X-Client-Id`
  - `X-Client-Secret`
- Service websocket:
  - required: `POST /v1/ws-token` then `WS /ws/events?ws_token=<token>`.
- Service secrets are hashed with PBKDF2-SHA256 format:
  - `pbkdf2_sha256$<iterations>$<salt_b64>$<digest_b64>`
- Legacy bcrypt hashes are still verifiable for backward compatibility.

## 5) Bot Access Enforcement
For bot-scoped service operations, app checks service access policy:
- if no `service_bot_access` rows exist for service: allow all enabled bots.
- if rows exist: allow only listed bot IDs.
- unauthorized use returns `403` with:
  - `Service is not allowed to access this bot account`

Endpoints enforcing this:
- `POST /v1/broadcaster-authorizations/start`
- `POST /v1/interests`
- `GET /v1/twitch/profiles`
- `GET /v1/twitch/streams/status`
- `POST /v1/twitch/chat/messages`

## 6) EventSub Routing Strategy
Routing decision (`EventSubManager._transport_for_event`):
- webhook-only Twitch event types always use webhook upstream.
- if webhook callback is configured, webhook is preferred upstream for supported types.
- if webhook callback is not configured, websocket is used as fallback for types that support websocket.

Interest transport in `service_interests` is independent and controls service delivery:
- `websocket`: publish to service websocket client (`/ws/events`).
- `webhook`: POST event envelope to service-provided callback URL.

## 7) Event Delivery Path
1. Twitch event arrives via upstream websocket or webhook callback.
2. Manager resolves bot ownership using `payload.subscription.id` against `twitch_subscriptions` (with fallback lookup for compatibility), then builds `InterestKey(bot_account_id, event_type, broadcaster_user_id)`.
3. Registry resolves matching interests.
4. Manager emits envelope:
   - to service websocket clients (per service ID), or
   - to service webhook URLs.
5. Runtime stats incremented:
   - API request count via `_service_auth`,
   - websocket connect/disconnect counts,
   - sent-event counts for websocket/webhook fanout.
6. Consumer-side hygiene expectation:
   - if a service receives webhook events it no longer wants, it should delete matching webhook interests.
   - this prevents stale interest rows from continuing webhook fanout.

Envelope format (current implementation):
```json
{
  "id": "<message-id>",
  "provider": "twitch",
  "type": "<event-type>",
  "event_timestamp": "<iso8601>",
  "event": {}
}
```

## 8) Stream State Behavior
- Interests auto-create default `stream.online` and `stream.offline` websocket interests for same `(service, bot, broadcaster)` on creation.
- Stream states are refreshed on startup for interested channels.
- `stream.online` / `stream.offline` notifications update `channel_states`.

## 9) Broadcaster Authorization Flow
1. Service calls `POST /v1/broadcaster-authorizations/start`.
2. API creates `broadcaster_authorization_requests` row and returns Twitch authorize URL.
3. Broadcaster consents on Twitch.
4. Twitch redirects to `GET /oauth/callback`.
5. Callback exchanges code, validates scope `channel:bot`, upserts `broadcaster_authorizations`.

## 10) Service User Authentication Flow
1. Service calls `POST /v1/user-auth/start`.
2. API creates `service_user_auth_requests` row and returns Twitch authorize URL with scope `user:read:email`.
3. End user consents on Twitch.
4. Twitch redirects to `GET /oauth/callback`.
5. Callback exchanges code, validates required scope, resolves user identity/email, and stores token/session fields in `service_user_auth_requests`.
6. Service reads result via `GET /v1/user-auth/session/{state}`.

## 11) Chat Send Behavior
Endpoint: `POST /v1/twitch/chat/messages`
- validates bot token and required scopes:
  - always: `user:write:chat`
  - for `auth_mode=app|auto`: `user:bot`
- `auth_mode`:
  - `auto`: app-token attempt first, then user-token fallback.
  - `app`: app-token only.
  - `user`: user-token only.
- response includes:
  - whether sent,
  - used mode,
  - bot badge eligibility hint,
  - drop reason if Twitch dropped message.

## 12) Clip Creation Behavior
Endpoint: `POST /v1/twitch/clips`
- validates service bot access and bot enabled state,
- validates bot OAuth scope `clips:edit`,
- passes `has_delay` through to Twitch Create Clip:
  - `true` -> buffered clip start (good for just-happened moments),
  - `false` -> live-edge clip start,
- calls Twitch Create Clip then polls Twitch Get Clips for up to 15 seconds,
- returns `status=ready` with URLs when available, otherwise `status=processing`.

## 13) CLI Responsibilities
`twitch-eventsub-cli console` provides operator workflows:
- bot OAuth setup/update and token refresh,
- service account management (create/regenerate/delete),
- service-to-bot access mapping management,
- webhook subscription listing/deletion,
- runtime/service usage status inspection,
- broadcaster authorization listing,
- live chat mode for bot own channel or target channel.

## 14) Webhook Replay Protection
- Incoming Twitch webhook requests are validated by:
  - signature (`message_id + timestamp + raw_body`),
  - timestamp freshness window.
- `Twitch-Eventsub-Message-Id` values are deduplicated in-memory for 10 minutes.
- Duplicate message IDs are acknowledged but ignored (no second processing/fanout).

## 15) Interest Creation Concurrency
- `service_interests` has a DB uniqueness constraint for dedupe dimensions.
- API creation paths (`POST /v1/interests` and automatic default stream interests) handle concurrent insert races by catching integrity conflicts and reusing existing rows.
