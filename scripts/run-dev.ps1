param(
  [int]$Port = 8080
)

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
  Write-Error "Cannot start dev mode. Missing or placeholder values in ${envPath}:`n - $($missing -join "`n - ")`nUpdate $envPath using $envExamplePath as reference."
  exit 1
}

$ngrokAuthtoken = Get-EnvValue -Key "NGROK_AUTHTOKEN"
if ([string]::IsNullOrWhiteSpace($ngrokAuthtoken)) {
  Write-Warning "NGROK_AUTHTOKEN is empty; ngrok tunnel will not be started."
}

docker compose up -d db
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not [string]::IsNullOrWhiteSpace($ngrokAuthtoken)) {
  Start-Process -NoNewWindow -FilePath "ngrok" -ArgumentList "http", $Port | Out-Null
  Write-Host "Started ngrok on port $Port"
}

uvicorn app.main:app --reload --host 0.0.0.0 --port $Port
exit $LASTEXITCODE
