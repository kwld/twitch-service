param(
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "scripts\cli-container.ps1"
& $scriptPath -Engine docker @CliArgs
exit $LASTEXITCODE
