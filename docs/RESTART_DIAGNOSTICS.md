# Restart Diagnostics

This note explains how to diagnose slow restart and slow `Gateway Subscriptions` loading after the recent EventSub startup changes.

## What To Look For

After restart, check logs from both:
- main repo server
- `twitch-service`

The system now emits timing logs for the major startup phases.

## Main Server Logs

Look for:

```text
[Services] twitchService.initialize completed in Xms
[Services] kickService.initialize completed in Xms
[Services] twitchGatewayService.initialize completed in Xms
[Services] streamStatusService.loadPersistentData completed in Xms
[Services] initServices completed in Xms
```

And after gateway websocket reconnect:

```text
[TwitchGateway] Resync completed in Xms for N channels.
```

Interpretation:
- high `twitchGatewayService.initialize` usually means remote gateway connect/setup is slow
- high `Resync completed` means channel replay is still a visible contributor
- high total `initServices` with low gateway time points elsewhere in the main app

## Twitch-Service Logs

Look for:

```text
EventSub phase load_interests completed in Xms
EventSub phase reconcile_from_twitch completed in Xms
EventSub phase ensure_authorization_revoke_webhook completed in Xms
EventSub phase ensure_webhook_subscriptions completed in Xms
EventSub manager startup finished in Xms
EventSub websocket session_welcome received: session_id=...
EventSub phase session_welcome_ensure_all_subscriptions completed in Xms
EventSub phase session_welcome_refresh_active_subscriptions completed in Xms
EventSub phase session_welcome_refresh_interested_channels completed in Xms
EventSub session_welcome bootstrap finished in Xms
Listed EventSub subscriptions across app+N bots: unique=M in Xms
EventSub reconcile persisted N subscriptions, skipped/merged D duplicates in Xms
Built DB EventSub subscription snapshot: rows=N in Xms
Built live EventSub subscription snapshot: upstream=N matched=M in Xms
```

Interpretation:
- high `Listed EventSub subscriptions across app+N bots` means Twitch upstream listing dominates
- high `reconcile_from_twitch` with moderate listing time means local dedupe/DB work dominates
- high `session_welcome_ensure_all_subscriptions` means re-ensure work for interests is the bottleneck
- high `Built live EventSub subscription snapshot` means `refresh=true` or forced live UI reads are expensive
- high `Built DB EventSub subscription snapshot` would indicate local DB volume/index issues, not Twitch API pressure

## Expected Startup Shape

Normal restart with websocket interests should now look like this:
1. load interests
2. reconcile from Twitch once
3. ensure webhook-only subscriptions
4. start manager loop
5. receive websocket `session_welcome`
6. ensure websocket subscriptions
7. run deferred stream-state refresh

Heavy stream-state refresh should not run both before and after `session_welcome` anymore.

## Expected UI Behavior

Normal `Gateway Subscriptions` view should use:
- `refresh=false`
- optional `broadcaster_user_id` filter
- reconciled local DB snapshot

That means a normal channel details open should no longer require a full live Twitch snapshot across all bot tokens.

Only explicit live refresh paths should pay the full Twitch enumeration cost.

## If Restart Is Still Slow

Use the logs to classify the bottleneck:
- upstream Twitch listing is slow: reduce forced `refresh=true` usage and inspect bot count/token health
- reconcile is slow: add DB indexes or further reduce duplicate work in `twitch_subscriptions`
- resync is slow: reduce monitored channel count or tighten diffing before `POST /v1/interests`
- session welcome ensure is slow: batch or coalesce ensures for identical broadcaster/event groups

## Recommended Next Checks

After one real restart, capture:
- total channel count restored by `twitchGatewayService`
- total enabled bot count in `twitch-service`
- `Listed EventSub subscriptions across app+N bots` duration
- `Resync completed in Xms for N channels`
- `EventSub manager startup finished in Xms`
- `EventSub session_welcome bootstrap finished in Xms`

That is enough to tell whether the remaining problem is:
- Twitch API latency
- bot count
- local DB work
- replay volume
- or frontend/admin polling behavior
