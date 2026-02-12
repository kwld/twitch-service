param(
  [ValidateSet("docker", "podman")][string]$Engine = "docker",
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".env")) {
  Write-Error "Missing .env file. Copy .env.example to .env and configure values."
  exit 1
}

$containerName = "twitch_eventsub_app_dev"
$composeFile = "docker-compose.dev.yml"
if (-not $CliArgs -or $CliArgs.Count -eq 0) {
  $CliArgs = @("console")
}

function Get-ExecTtyFlags {
  if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
    return @("-i")
  }
  return @("-it")
}

if ($Engine -eq "docker") {
  $running = docker ps --filter "name=^$containerName$" --format "{{.Names}}"
  if ($running -ne $containerName) {
    docker compose -f $composeFile up -d db app
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
  $ttyFlags = Get-ExecTtyFlags
  $dockerExecArgs = @("exec")
  $dockerExecArgs += $ttyFlags
  $dockerExecArgs += $containerName
  $dockerExecArgs += "twitch-eventsub-cli"
  $dockerExecArgs += $CliArgs
  & docker @dockerExecArgs
  exit $LASTEXITCODE
}

$running = podman ps --filter "name=^$containerName$" --format "{{.Names}}"
if ($running -ne $containerName) {
  podman compose -f $composeFile up -d db app
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$ttyFlags = Get-ExecTtyFlags
$podmanExecArgs = @("exec")
$podmanExecArgs += $ttyFlags
$podmanExecArgs += $containerName
$podmanExecArgs += "twitch-eventsub-cli"
$podmanExecArgs += $CliArgs
& podman @podmanExecArgs
exit $LASTEXITCODE
