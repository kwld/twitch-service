param(
  [ValidateSet("docker", "podman")][string]$Engine = "docker",
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".env")) {
  Write-Error "Missing .env file. Copy .env.example to .env and configure values."
  exit 1
}

if (-not $CliArgs -or $CliArgs.Count -eq 0) {
  $CliArgs = @("console")
}

Write-Host "[twitch-service] Opening CLI against the LIVE stack (docker-compose.yml)." -ForegroundColor Cyan

if ($Engine -eq "docker") {
  & docker compose -f "docker-compose.yml" exec app twitch-eventsub-cli @CliArgs
  exit $LASTEXITCODE
}

& podman compose -f "docker-compose.yml" exec app twitch-eventsub-cli @CliArgs
exit $LASTEXITCODE
