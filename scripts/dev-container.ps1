param(
  [ValidateSet("docker", "podman")][string]$Engine = "docker",
  [switch]$Build
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".env")) {
  Write-Error "Missing .env file. Copy .env.example to .env and configure values."
  exit 1
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
