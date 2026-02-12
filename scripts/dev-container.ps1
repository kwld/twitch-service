param(
  [ValidateSet("docker", "podman")][string]$Engine = "docker",
  [switch]$Build
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".env")) {
  Write-Error "Missing .env file. Copy .env.example to .env and configure values."
  exit 1
}

$envText = Get-Content ".env" -Raw
if ($envText -notmatch "(?m)^NGROK_AUTHTOKEN=.+$") {
  Write-Warning "NGROK_AUTHTOKEN is missing or empty in .env. ngrok container may fail to connect."
}

$composeArgs = @("-f", "docker-compose.dev.yml", "up", "-d")
if ($Build) {
  $composeArgs += "--build"
}

if ($Engine -eq "docker") {
  & docker compose @composeArgs
  exit $LASTEXITCODE
}

& podman compose @composeArgs
exit $LASTEXITCODE
