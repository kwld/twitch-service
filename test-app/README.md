# Test App (Node.js)

This is a full-coverage integration tester for the Twitch service in this repo.

It exercises:
- admin auth + bot listing,
- service account creation/list/regeneration (as needed),
- service-auth API calls,
- service-visible accessible-bot discovery (`GET /v1/bots/accessible`),
- EventSub catalog discovery,
- broadcaster authorization flow bootstrap,
- interest create/list/heartbeat/delete,
- websocket event listening (`/ws/events`),
- optional webhook transport receive test,
- Twitch profile and stream-status reads,
- chat message send via `POST /v1/twitch/chat/messages`.

## 1) Setup
1. Copy `.env.example` to `.env`.
2. Fill required fields:
   - `SERVICE_BASE_URL`
   - `ADMIN_API_KEY`
   - `TEST_BROADCASTER_IDS`
3. Optional:
   - `TEST_BOT_ID` or `TEST_BOT_NAME` to pick a specific bot.
   - `TEST_WEBHOOK_PUBLIC_URL` to validate webhook transport (point to `/service-webhook`).

Install dependencies:
```bash
cd test-app
npm install
```

## 2) Run Full Scenario
```bash
npm run full
```

The run:
- picks a bot,
- ensures service credentials (env, cached file, or admin create/regenerate),
- starts broadcaster authorization if missing and prints Twitch `authorize_url`,
- opens websocket listener,
- creates chat + online interests (and webhook offline interest if configured),
- heartbeats interests while listening,
- sends a test chat message,
- optionally cleans created interests (default: yes).

## 3) Credential Cache
The app writes a local cache file:
- `test-app/.service-account.json`

It stores `client_id` + current `client_secret` for reuse.

## 4) Notes
- For broadcaster/channel authorization, the streamer must complete Twitch consent in browser.
- If broadcaster grant is missing, Twitch can return:
  - `403 subscription missing proper authorization`
- For full chat capability, bot OAuth should include:
  - `user:bot`
  - `user:read:chat`
  - `user:write:chat`
- Broadcaster must authorize:
  - `channel:bot`
