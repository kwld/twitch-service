# Architecture And Runtime Behavior

This document describes how the application works from process startup to event delivery.

## 1) Core Components
- `app/main.py`: FastAPI app, API endpoints, auth dependencies, lifecycle startup/shutdown.
- `app/eventsub_manager.py`: reconciles and maintains upstream Twitch EventSub subscriptions.
- `app/event_router.py`:
  - `InterestRegistry`: in-memory map of service interests.
  - `LocalEventHub`: local fanout over websocket and outgoing webhooks.
- `app/twitch.py`: Twitch OAuth/Helix client wrapper.
- `app/cli.py`: async operator console (bot setup, service account management, live chat tools).
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
- `oauth_callbacks`: callback relay storage for CLI OAuth polling.
- `service_runtime_stats`: per-service counters and connection/event timestamps.

## 4) Authentication Model
- Admin endpoints: `X-Admin-Key` must match `ADMIN_API_KEY`.
- Service endpoints:
  - `X-Client-Id`
  - `X-Client-Secret`
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
- `user.authorization.revoke`: always webhook upstream.
- if event type is listed in `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES`: webhook upstream.
- otherwise: websocket upstream.

Interest transport in `service_interests` is independent and controls service delivery:
- `websocket`: publish to service websocket client (`/ws/events`).
- `webhook`: POST event envelope to service-provided callback URL.

## 7) Event Delivery Path
1. Twitch event arrives via upstream websocket or webhook callback.
2. Manager builds `InterestKey(bot_account_id, event_type, broadcaster_user_id)`.
3. Registry resolves matching interests.
4. Manager emits envelope:
   - to service websocket clients (per service ID), or
   - to service webhook URLs.
5. Runtime stats incremented:
   - API request count via `_service_auth`,
   - websocket connect/disconnect counts,
   - sent-event counts for websocket/webhook fanout.

Envelope format (current implementation):
```json
{
  "id": "<message-id>",
  "type": "<event-type>",
  "event_timestamp": "<iso8601>",
  "event": {}
}
```

## 8) Stream State Behavior
- Interests auto-create default `channel.online` and `channel.offline` websocket interests for same `(service, bot, broadcaster)` on creation.
- Stream states are refreshed on startup for interested channels.
- `channel.online` / `channel.offline` notifications update `channel_states`.

## 9) Broadcaster Authorization Flow
1. Service calls `POST /v1/broadcaster-authorizations/start`.
2. API creates `broadcaster_authorization_requests` row and returns Twitch authorize URL.
3. Broadcaster consents on Twitch.
4. Twitch redirects to `GET /oauth/callback`.
5. Callback exchanges code, validates scope `channel:bot`, upserts `broadcaster_authorizations`.

## 10) Chat Send Behavior
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

## 11) CLI Responsibilities
`twitch-eventsub-cli console` provides operator workflows:
- bot OAuth setup/update and token refresh,
- service account management (create/regenerate/delete),
- service-to-bot access mapping management,
- webhook subscription listing/deletion,
- runtime/service usage status inspection,
- broadcaster authorization listing,
- live chat mode for bot own channel or target channel.
