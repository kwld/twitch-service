param()

$ErrorActionPreference = "Stop"

$envPath = ".env"
$envExamplePath = ".env.example"
$requiredKeys = @(
  "TWITCH_CLIENT_ID",
  "TWITCH_CLIENT_SECRET",
  "TWITCH_REDIRECT_URI",
  "TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL",
  "ADMIN_API_KEY",
  "SERVICE_SIGNING_SECRET"
)

function Get-EnvValue {
  param(
    [Parameter(Mandatory = $true)][string]$Key
  )

  if (!(Test-Path $envPath)) {
    return ""
  }

  $pattern = "^{0}=(.*)$" -f [regex]::Escape($Key)
  $line = Get-Content $envPath | Where-Object { $_ -match $pattern } | Select-Object -Last 1
  if ($null -eq $line) {
    return ""
  }

  return ($line -replace $pattern, '$1')
}

function Test-MissingOrPlaceholder {
  param([string]$Value)

  if ([string]::IsNullOrWhiteSpace($Value)) {
    return $true
  }

  return $Value -match "^replace_me"
}

function Test-LokiEnabled {
  $hostValue = Get-EnvValue -Key "LOKI_HOST"
  $portValue = Get-EnvValue -Key "LOKI_PORT"
  if (Test-MissingOrPlaceholder -Value $hostValue) { return $false }
  if (Test-MissingOrPlaceholder -Value $portValue) { return $false }
  return $true
}

if (!(Test-Path $envPath)) {
  if (!(Test-Path $envExamplePath)) {
    Write-Error "Missing $envExamplePath; cannot bootstrap $envPath."
    exit 1
  }
  Copy-Item $envExamplePath $envPath
  Write-Host "Created $envPath from $envExamplePath"
}

$missing = @()
foreach ($key in $requiredKeys) {
  $value = Get-EnvValue -Key $key
  if (Test-MissingOrPlaceholder -Value $value) {
    $missing += $key
  }
}

if ($missing.Count -gt 0) {
  Write-Warning "Environment setup required in ${envPath}:"
  foreach ($key in $missing) {
    Write-Warning "  - $key"
  }
  Write-Warning "Update $envPath with real values (use $envExamplePath as reference)."
}

python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pip install -e .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path "test-app/package.json") {
  Push-Location test-app
  npm install
  $npmExit = $LASTEXITCODE
  Pop-Location
  if ($npmExit -ne 0) { exit $npmExit }
}

Write-Host "Install complete."

if (Get-Command docker -ErrorAction SilentlyContinue) {
  if (Test-LokiEnabled) {
    Write-Host "Loki config detected. Pulling Alloy image..."
    docker compose -f docker-compose.yml pull alloy
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
}
