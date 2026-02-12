param(
  [ValidateSet("docker", "podman")][string]$Engine = "docker",
  [switch]$Build
)

$ErrorActionPreference = "Stop"

$envPath = ".env"
$envExamplePath = ".env.example"

function Get-EnvValue {
  param(
    [Parameter(Mandatory = $true)][string]$Key,
    [string]$Default = ""
  )

  if (!(Test-Path $envPath)) {
    return $Default
  }

  $pattern = "^{0}=(.*)$" -f [regex]::Escape($Key)
  $line = Get-Content $envPath | Where-Object { $_ -match $pattern } | Select-Object -Last 1
  if ($null -eq $line) {
    return $Default
  }
  return ($line -replace $pattern, '$1')
}

function Set-EnvValue {
  param(
    [Parameter(Mandatory = $true)][string]$Key,
    [Parameter(Mandatory = $true)][string]$Value
  )

  $pattern = "^{0}=.*$" -f [regex]::Escape($Key)
  $lines = @()
  if (Test-Path $envPath) {
    $lines = @(Get-Content $envPath)
  }

  $updated = $false
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match $pattern) {
      $lines[$i] = "$Key=$Value"
      $updated = $true
      break
    }
  }

  if (-not $updated) {
    $lines += "$Key=$Value"
  }

  Set-Content -Path $envPath -Value $lines
}

function New-RandomHex {
  param([int]$Bytes = 32)
  $buffer = New-Object byte[] $Bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
  return -join ($buffer | ForEach-Object { $_.ToString("x2") })
}

if (!(Test-Path $envPath)) {
  if (!(Test-Path $envExamplePath)) {
    Write-Error "Missing .env.example; cannot bootstrap .env."
    exit 1
  }
  Copy-Item $envExamplePath $envPath
  Write-Host "Created .env from .env.example"
}

Set-EnvValue -Key "APP_ENV" -Value "prod"

$postgresDb = Get-EnvValue -Key "POSTGRES_DB" -Default "twitch_eventsub"
$postgresUser = Get-EnvValue -Key "POSTGRES_USER" -Default "twitch"
$postgresPassword = Get-EnvValue -Key "POSTGRES_PASSWORD" -Default "twitch"
$databaseUrl = "postgresql+asyncpg://$postgresUser`:$postgresPassword@db:5432/$postgresDb"
Set-EnvValue -Key "DATABASE_URL" -Value $databaseUrl

$adminApiKey = Get-EnvValue -Key "ADMIN_API_KEY"
if ([string]::IsNullOrWhiteSpace($adminApiKey) -or $adminApiKey -match "^replace_me") {
  Set-EnvValue -Key "ADMIN_API_KEY" -Value (New-RandomHex -Bytes 24)
  Write-Host "Generated ADMIN_API_KEY"
}

$serviceSigningSecret = Get-EnvValue -Key "SERVICE_SIGNING_SECRET"
if ([string]::IsNullOrWhiteSpace($serviceSigningSecret) -or $serviceSigningSecret -match "^replace_me") {
  Set-EnvValue -Key "SERVICE_SIGNING_SECRET" -Value (New-RandomHex -Bytes 32)
  Write-Host "Generated SERVICE_SIGNING_SECRET"
}

$webhookSecret = Get-EnvValue -Key "TWITCH_EVENTSUB_WEBHOOK_SECRET"
if ([string]::IsNullOrWhiteSpace($webhookSecret) -or $webhookSecret -match "^replace_me") {
  Set-EnvValue -Key "TWITCH_EVENTSUB_WEBHOOK_SECRET" -Value (New-RandomHex -Bytes 24)
  Write-Host "Generated TWITCH_EVENTSUB_WEBHOOK_SECRET"
}

$twitchClientId = Get-EnvValue -Key "TWITCH_CLIENT_ID"
$twitchClientSecret = Get-EnvValue -Key "TWITCH_CLIENT_SECRET"
if ($twitchClientId -match "^replace_me" -or $twitchClientSecret -match "^replace_me") {
  Write-Warning "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET still use placeholder values."
}

$composeArgs = @("-f", "docker-compose.yml", "up", "-d", "--remove-orphans")
if ($Build) {
  $composeArgs += "--build"
}

if ($Engine -eq "docker") {
  & docker compose @composeArgs
  exit $LASTEXITCODE
}

& podman compose @composeArgs
exit $LASTEXITCODE
