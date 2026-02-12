# Production Deployment (Docker or Podman)

This guide is for running the service directly from a fresh clone.

## 1) Prerequisites
- Docker Engine + Compose plugin, or Podman + `podman compose`
- Git
- Public HTTPS domain for Twitch callbacks

## 2) Fresh clone
```bash
git clone <your-repo-url> twitch-service
cd twitch-service
```

## 3) Start production stack
Docker:
```powershell
./scripts/prod-container.ps1 -Engine docker -Build
```
or
```bash
./scripts/prod-container.sh docker --build
```

Podman:
```powershell
./scripts/prod-container.ps1 -Engine podman -Build
```
or
```bash
./scripts/prod-container.sh podman --build
```

What the script does:
- creates `.env` from `.env.example` if missing,
- sets `APP_ENV=prod`,
- rewrites `DATABASE_URL` to use `db` container networking,
- auto-generates secure values for:
  - `ADMIN_API_KEY`
  - `SERVICE_SIGNING_SECRET`
  - `TWITCH_EVENTSUB_WEBHOOK_SECRET` (if placeholder),
- starts `docker-compose.yml` in detached mode.

## 4) Set required Twitch values
Edit `.env` and set:
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`
- `TWITCH_REDIRECT_URI` (must match your Twitch app setting)
- `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL` (public HTTPS URL ending with `/webhooks/twitch/eventsub`)

Then restart:

Docker:
```bash
docker compose -f docker-compose.yml up -d --build
```

Podman:
```bash
podman compose -f docker-compose.yml up -d --build
```

## 5) Verify deployment
- Health endpoint: `GET http://<host>:8080/health`
- Logs:
  - Docker: `docker compose -f docker-compose.yml logs -f app`
  - Podman: `podman compose -f docker-compose.yml logs -f app`

## 6) Open CLI inside running app container
Docker:
```bash
docker compose -f docker-compose.yml exec app twitch-eventsub-cli console
```

Podman:
```bash
podman compose -f docker-compose.yml exec app twitch-eventsub-cli console
```

## 7) Stop stack
Docker:
```bash
docker compose -f docker-compose.yml down
```

Podman:
```bash
podman compose -f docker-compose.yml down
```
