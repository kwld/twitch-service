param(
  [Parameter(Mandatory = $true)][string]$RemoteHost,
  [string]$RemotePath = "/opt/twitch-eventsub-service"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".env")) {
  Write-Error "Missing .env file. Copy .env.example to .env and configure values."
  exit 1
}

Write-Host "Syncing files to $RemoteHost`:$RemotePath ..."
rsync -az --delete `
  --exclude ".git" `
  --exclude ".venv" `
  --exclude "__pycache__" `
  --exclude ".pytest_cache" `
  ./ "$RemoteHost`:$RemotePath"

Write-Host "Running remote docker compose deploy..."
ssh $RemoteHost "cd $RemotePath && docker compose pull && docker compose build --pull && docker compose up -d --remove-orphans"

Write-Host "Deployment finished."
