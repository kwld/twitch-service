param(
  [int]$Port = 8080,
  [string]$NgrokPath = "ngrok"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".env")) {
  Write-Error "Missing .env file. Copy .env.example to .env and configure values."
  exit 1
}

Write-Host "Starting Postgres via docker compose..."
docker compose up -d db
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Starting ngrok tunnel..."
Start-Process -NoNewWindow -FilePath $NgrokPath -ArgumentList "http", $Port

Write-Host "Starting API with live reload..."
uvicorn app.main:app --reload --host 0.0.0.0 --port $Port
