# Twitch EventSub Service

`twitch-service` is a standalone Twitch integration service focused on authentication, EventSub lifecycle management, and Twitch-facing helper APIs.

## Responsibilities

The service is responsible for:

- storing bot OAuth credentials and broadcaster authorizations in PostgreSQL
- managing deduplicated EventSub interests and active Twitch subscriptions
- selecting and maintaining the correct upstream transport for each EventSub type
- receiving Twitch events over WebSocket or webhook
- delivering events to authenticated local clients over service WebSocket or webhook integrations
- exposing Twitch helper APIs for chat, moderation, clip creation, profile lookup, and stream status
- providing an operator CLI for OAuth, broadcaster authorization, subscription inspection, and live diagnostics

## Core Concepts

### Bot Accounts

Bot accounts are Twitch user accounts authenticated through OAuth and stored by the service.

They are used for:

- chat send APIs
- moderation APIs
- Twitch profile and stream lookup helpers
- EventSub subscriptions that require bot-based authorization

### Broadcaster Authorizations

Some EventSub types and Twitch actions require authorization from the broadcaster channel itself.

The service stores those grants separately from bot OAuth and uses them when:

- a subscription type must be bound to broadcaster authorization
- a bot action depends on channel-level consent

### Interests

An interest is the service's internal declaration that a client wants a specific Twitch event for a specific broadcaster.

The service:

- persists interests
- deduplicates equivalent interests
- reconciles them against live Twitch subscriptions
- removes stale interests when heartbeats stop

### Authorization Sources

The current persisted authorization sources are:

- `broadcaster`
- `bot_moderator`

`bot_moderator` is only valid for subscription types that actually support moderator-user-bound authorization and the required transport path. That logic is enforced in:

- `app/eventsub_authorization.py`
- `app/eventsub_catalog.py`

## Transport Model

The service can consume Twitch EventSub from:

- Twitch WebSocket transport
- Twitch webhook transport

It can then fan events out to local consumers through:

- service WebSocket delivery
- outgoing service webhooks

Transport selection is based on event capabilities and current service configuration, not just a fixed global mode.

## Main Features

- interactive CLI for bot setup, token refresh, broadcaster authorization, and subscription inspection
- runtime status dashboard
- service-authenticated EventSub interest registration
- active subscription inspection endpoints
- Twitch chat send helper
- Twitch moderation helpers
- Twitch clip creation helper
- Twitch user/profile lookup helpers
- Twitch stream status helpers
- signed Twitch webhook callback handling
- startup reconciliation of persisted interests and live Twitch subscriptions

## Service CLI

The service ships with an operator CLI exposed through `twitch-eventsub-cli`.

Typical uses:

- guided bot OAuth setup
- refresh token validation
- broadcaster authorization flow
- active subscription inspection
- service status inspection
- live communication tracing

### Live CLI

Use the live stack helper when operating on production:

```bash
./scripts/cli-live.sh docker
```

### Dev CLI

Use the dev helper when operating on the development compose stack:

```bash
./scripts/cli-container.sh
```

These are intentionally different because live and dev use different compose files and different databases.

## OAuth and Scope Model

The service supports two major OAuth flows:

- bot-account OAuth
- broadcaster authorization

For moderation-heavy setups, verify both:

- the bot token scopes
- the broadcaster grant required by the target action or subscription

The live guided bot setup requests the current moderation-oriented scope bundle, including:

- `channel:moderate`
- `moderator:manage:banned_users`
- `moderator:manage:chat_messages`
- `moderator:manage:chat_settings`
- `moderator:manage:blocked_terms`
- `moderator:manage:automod`
- `moderator:read:banned_users`
- `moderator:read:chat_messages`
- `moderator:read:chat_settings`
- `moderator:read:blocked_terms`
- `moderator:read:moderators`
- `moderator:read:vips`
- `moderator:read:warnings`
- `moderator:read:unban_requests`

## HTTP and WebSocket Surfaces

### Health and Status

- `GET /health`
- `GET /status`
- `POST /status`
- `WS /ws/status`

### OAuth and Twitch Callback Handling

- `GET /oauth/callback`
- `POST /webhooks/twitch/eventsub`

### EventSub and Interest Management

- `POST /v1/interests`
- `DELETE /v1/interests/{interest_id}`
- `POST /v1/interests/{interest_id}/heartbeat`
- `GET /v1/eventsub/subscription-types`
- `GET /v1/eventsub/subscriptions/active`

### Service Delivery

- `POST /v1/ws-token`
- `WS /ws/events`

### Twitch Helper APIs

- `GET /v1/twitch/profiles`
- `GET /v1/twitch/streams/status`
- `POST /v1/twitch/chat/messages`
- `POST /v1/twitch/clips`

## Local Development

Basic local flow:

1. copy `.env.example` to `.env`
2. start the database
3. run migrations
4. start the API
5. use the CLI to add a bot or authorize a broadcaster

Typical commands:

```bash
docker compose up -d db
python -m alembic upgrade head
twitch-eventsub-api
twitch-eventsub-cli console
```

## Operational Checks

When diagnosing a service issue, verify in this order:

1. service health
2. bot token validity and scopes
3. broadcaster authorization state
4. active interest for the broadcaster and event type
5. active EventSub subscription state
6. incoming Twitch delivery on the service
7. outgoing delivery from the service to the local client

## File Map

Useful code entry points:

- `app/config.py`
- `app/eventsub_authorization.py`
- `app/eventsub_catalog.py`
- `app/routes/`
- `app/eventsub_manager_parts/`
- `scripts/cli-live.sh`
- `scripts/cli-container.sh`

## Source Of Truth

If older archived notes disagree with this file, treat this README and the current code as the source of truth for `twitch-service`.
