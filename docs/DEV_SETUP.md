# Development Container Setup (Windows + Docker/Podman)

This guide runs the service, Postgres, and ngrok in containers with hot reload.

## 1) Prerequisites
- Docker Desktop or Podman Desktop
- PowerShell
- ngrok account + auth token

## 2) Configure environment
1. Copy `.env.example` to `.env`.
2. Fill required values for Twitch and DB.
3. Set ngrok token:
   - `NGROK_AUTHTOKEN=<your token>`

If `.env` is missing, startup scripts fail immediately.

## 3) Start development containers
Docker:
```powershell
./scripts/dev-container.ps1 -Engine docker -Build
```

Podman:
```powershell
./scripts/dev-container.ps1 -Engine podman -Build
```

This starts:
- `db` (Postgres on `localhost:5432`)
- `app` (API on `localhost:8080`, hot reload enabled)
- `ngrok` (inspector on `http://localhost:4040`)

## 4) Get the public ngrok URL
Open `http://localhost:4040` and copy the HTTPS forwarding URL.

For mixed upstream transport, set in `.env`:
- `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL=<ngrok-https-url>/webhooks/twitch/eventsub`
- `TWITCH_EVENTSUB_WEBHOOK_SECRET=<10-100 chars>`
- `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES=channel.online,channel.offline`

Events listed in `TWITCH_EVENTSUB_WEBHOOK_EVENT_TYPES` use webhook.
All other events use websocket.

Restart containers after `.env` changes:
```powershell
docker compose -f docker-compose.dev.yml down
docker compose -f docker-compose.dev.yml up -d --build
```

## 5) Verify service
- Health: `http://localhost:8080/health`
- ngrok inspector: `http://localhost:4040`

## 6) Stop containers
Docker:
```powershell
docker compose -f docker-compose.dev.yml down
```

Podman:
```powershell
podman compose -f docker-compose.dev.yml down
```
