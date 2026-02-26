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

## 3) Prepare environment and dependencies
Use helper install script (guides `.env` setup when values are missing):

PowerShell:
```powershell
./scripts/install.ps1
```

Bash:
```bash
bash ./scripts/install.sh
```

## 4) Set required Twitch values
Edit `.env` and set:
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`
- `TWITCH_REDIRECT_URI` (must match your Twitch app setting)
- `TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL` (public HTTPS URL ending with `/webhooks/twitch/eventsub`)

Recommended DB pool settings for production burst traffic:
- `DB_POOL_SIZE=20`
- `DB_MAX_OVERFLOW=20`
- `DB_POOL_TIMEOUT_SECONDS=30`
- `DB_POOL_RECYCLE_SECONDS=1800`

## 5) Start production stack

Docker:
```bash
docker compose -f docker-compose.yml up -d --build
```

Podman:
```bash
podman compose -f docker-compose.yml up -d --build
```

Note:
- The app container now runs `python -m alembic upgrade head` before starting the API process.

## 6) Verify deployment
- Health endpoint: `GET http://<host>:8080/health`
- Logs:
  - Docker: `docker compose -f docker-compose.yml logs -f app`
  - Podman: `podman compose -f docker-compose.yml logs -f app`

## 7) Open CLI inside running app container
Docker:
```bash
docker compose -f docker-compose.yml exec app twitch-eventsub-cli console
```

Podman:
```bash
podman compose -f docker-compose.yml exec app twitch-eventsub-cli console
```

## 8) Stop stack
Docker:
```bash
docker compose -f docker-compose.yml down
```

Podman:
```bash
podman compose -f docker-compose.yml down
```

## 9) Proper Start/Restart Procedure (Existing Production Install)
From existing install directory (example: `/var/www/twitch-service`):

```bash
cd /var/www/twitch-service
git fetch --all --prune
git pull --ff-only origin main
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml logs --tail=200 app
```

Health checks:

```bash
curl -fsS http://127.0.0.1:8080/health
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml exec -T db psql -U ${POSTGRES_USER:-twitch} -d ${POSTGRES_DB:-twitch_eventsub} -c "select state, count(*) from pg_stat_activity where datname='${POSTGRES_DB:-twitch_eventsub}' group by state order by state;"
```
