param()

$ErrorActionPreference = "Stop"

$envPath = ".env"
$envExamplePath = ".env.example"
$requiredKeys = @(
  "DATABASE_URL",
  "ADMIN_API_KEY",
  "SERVICE_SIGNING_SECRET",
  "TWITCH_CLIENT_ID",
  "TWITCH_CLIENT_SECRET"
)

function Get-EnvValue {
  param(
    [Parameter(Mandatory = $true)][string]$Key
  )

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
  Write-Error "Missing $envPath. Copy $envExamplePath to $envPath and fill required values."
  exit 1
}

$missing = @()
foreach ($key in $requiredKeys) {
  $value = Get-EnvValue -Key $key
  if (Test-MissingOrPlaceholder -Value $value) {
    $missing += $key
  }
}

if ($missing.Count -gt 0) {
  Write-Error "Cannot start. Missing or placeholder values in ${envPath}:`n - $($missing -join "`n - ")`nUpdate $envPath using $envExamplePath as reference."
  exit 1
}

if (Test-LokiEnabled) {
  if (!(Test-Path "logs")) {
    New-Item -ItemType Directory -Path "logs" | Out-Null
  }
  docker compose up -d db alloy
}
else {
  docker compose up -d db
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

twitch-eventsub-api
exit $LASTEXITCODE
