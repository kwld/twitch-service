param(
  [ValidateSet("docker", "podman")][string]$Engine = "docker"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".env")) {
  Write-Error "Missing .env file. Copy .env.example to .env and configure values."
  exit 1
}

$containerName = "twitch_eventsub_app_dev"
$composeFile = "docker-compose.dev.yml"

if ($Engine -eq "docker") {
  $running = docker ps --filter "name=^$containerName$" --format "{{.Names}}"
  if ($running -eq $containerName) {
    docker exec -it $containerName twitch-eventsub-cli console
    exit $LASTEXITCODE
  }
  docker compose -f $composeFile run --rm app twitch-eventsub-cli console
  exit $LASTEXITCODE
}

$running = podman ps --filter "name=^$containerName$" --format "{{.Names}}"
if ($running -eq $containerName) {
  podman exec -it $containerName twitch-eventsub-cli console
  exit $LASTEXITCODE
}
podman compose -f $composeFile run --rm app twitch-eventsub-cli console
exit $LASTEXITCODE
