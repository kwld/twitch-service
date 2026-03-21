# Twitch EventSub Service

Operator-facing overview for the current `twitch-service` state used by `gemini-bot-flow-studio`.

## What The Service Does

The service is a dedicated Twitch sidecar that:

- stores bot OAuth credentials and broadcaster grants in PostgreSQL
- keeps deduplicated EventSub interests and subscriptions
- receives Twitch EventSub over WebSocket or webhook
- exposes internal APIs for chat, moderation, clips, user lookup, stream status, and service event delivery
- provides an operator CLI for OAuth, broadcaster authorization, subscription inspection, and runtime diagnostics

The service does not execute BotScript. Command execution and `events/*.script` execution belong to `botscript-service`.

## Production Model

Current production flow:

1. services in the monorepo register or refresh interests
2. `twitch-service` reconciles those interests with Twitch EventSub
3. Twitch events arrive through the transport selected for that event type
4. downstream services consume those events over the service API / websocket path

In the main monorepo this usually means:

- `server/` owns orchestration and persistence
- `botscript-service/` owns execution
- `twitch-service/` owns Twitch-facing auth, subscriptions, and event delivery

## Authorization Sources

The current service supports these persisted authorization sources:

- `broadcaster`
- `bot_moderator`

Important rule:

- `bot_moderator` is only valid for subscription types that actually require `moderator_user_id` and support the service's WebSocket path

This logic lives in:

- `app/eventsub_authorization.py`
- `app/eventsub_catalog.py`

Practical consequence:

- `channel.moderate` can run through `bot_moderator` when the bot is a moderator and has the required scopes
- `channel.ban` and `channel.unban` are not generic "moderator means allowed" events and must follow the correct broadcaster-bound authorization model

## Live Vs Dev CLI

Use the correct script for the correct database.

Live:

```bash
./scripts/cli-live.sh docker
```

Dev:

```bash
./scripts/cli-container.sh
```

Why this matters:

- the live script targets `docker-compose.yml`
- the dev script targets `docker-compose.dev.yml`
- OAuth done through the dev CLI updates the dev database only

If a bot was re-authorized but the live stack still shows old scopes, the first thing to verify is whether OAuth was completed through the live CLI.

## Common Operator Tasks

### Re-authorize a bot on live

```bash
cd /var/www/twitch-service
./scripts/cli-live.sh docker
```

Then choose:

- `2) Guided bot setup (OAuth wizard)`

### Inspect active subscriptions

Use the CLI:

- `9) Manage active EventSub subscriptions`

Or the API:

- `GET /v1/eventsub/subscriptions/active`

### Inspect broadcaster authorizations

Use the CLI:

- `11) View broadcaster authorizations`

### Verify service health

```bash
curl http://127.0.0.1:18081/health
```

## Main Endpoints

- `GET /health`
- `GET /status`
- `POST /status`
- `WS /ws/status`
- `GET /oauth/callback`
- `POST /webhooks/twitch/eventsub`
- `POST /v1/ws-token`
- `GET /v1/eventsub/subscription-types`
- `GET /v1/eventsub/subscriptions/active`
- `POST /v1/interests`
- `POST /v1/interests/{interest_id}/heartbeat`
- `GET /v1/twitch/profiles`
- `GET /v1/twitch/streams/status`
- `POST /v1/twitch/chat/messages`
- `POST /v1/twitch/clips`
- `WS /ws/events`

## Scope Expectations

For moderation-heavy setups, verify both:

- bot OAuth scopes
- broadcaster authorization state

The live wizard requests the broader moderator bundle needed by the current stack, including:

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

## Notes For This Repo

- main monorepo docs live under `../docs/`
- archived historical docs live under `../.docs_old/`
- if this README and archived markdown disagree, treat this README and the current code as the source of truth
