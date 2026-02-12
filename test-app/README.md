# Test App (Frontend + Node Backend)

This is a simple browser UI for testing the Twitch service end-to-end with service credentials.

What it can test:
- list accessible bots (`GET /v1/bots/accessible`),
- resolve broadcaster ID from username,
- start broadcaster grant flow (`POST /v1/broadcaster-authorizations/start`),
- view broadcaster grants,
- create/list/heartbeat/delete interests,
- connect/disconnect service websocket events (`/ws/events`) and view live event log,
- receive webhook events at `/service-webhook`,
- send chat messages as bot (`POST /v1/twitch/chat/messages`).

## 1) Setup
1. Copy `.env.example` to `.env`.
2. Fill required fields:
   - `SERVICE_BASE_URL`
   - `SERVICE_CLIENT_ID`
   - `SERVICE_CLIENT_SECRET`
3. Optional:
   - `TEST_APP_PORT` (default `9090`)
   - `TEST_WEBHOOK_PUBLIC_URL` (public URL ending with `/service-webhook`)

Install dependencies:
```bash
cd test-app
npm install
```

## 2) Run
```bash
npm start
```

Open:
- `http://localhost:9090` (or your configured `TEST_APP_PORT`)

## 3) Typical Flow in UI
1. Refresh status and bots.
2. Select bot and fill broadcaster user id.
3. Optional: fill broadcaster username and click `Resolve Username To ID`.
4. Click `Start Broadcaster Grant`, complete Twitch consent in browser.
   - the app sends `redirect_url` and receives callback result back to the same UI.
   - on success, broadcaster id/login fields are auto-filled from callback query params.
5. Connect service websocket.
6. Create interest `channel.chat.message` with `websocket`.
7. Send chat message.
8. Watch incoming chat/events in the live log.

For webhook transport tests:
1. Set `TEST_WEBHOOK_PUBLIC_URL` in `.env`.
2. Create an interest with `transport=webhook`.
3. Trigger event and observe `[webhook]` entries in live log.
