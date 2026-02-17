# EventSub Transport Catalog (Twitch Upstream)

This document tracks upstream Twitch EventSub transport capability used by this bridge.

Source of truth:
- https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/

Snapshot date:
- 2026-02-17

## Bridge Policy
- Local services choose only downstream delivery transport from this bridge:
  - `websocket` via `WS /ws/events`
  - `webhook` via per-interest `webhook_url`
- Bridge chooses upstream Twitch transport automatically:
  - if webhook callback is configured, upstream webhook is preferred,
  - if webhook callback is unavailable, upstream websocket is used when supported,
  - webhook-only Twitch events always remain upstream webhook.

## Twitch Capability Catalog
Per Twitch docs, these event types are webhook-only (cannot use WebSockets):
- `drop.entitlement.grant`
- `extension.bits_transaction.create`
- `user.authorization.grant`
- `user.authorization.revoke`

All other event types in the current `app/eventsub_catalog.py` snapshot support both Twitch webhook and Twitch websocket transports.

## Bot-Oriented Auth Notes
For upstream EventSub subscription creation:
- Twitch webhook transport uses app access token.
- Twitch websocket transport uses user access token (bot account token in this service).

Reference:
- https://dev.twitch.tv/docs/chat/authenticating/
