# Local health plus optional live probes. Never prints a key, a database URL, or a request URL.
# Reads the process environment first, then the repo .env for names the process does not define.
# Run from any directory. Default is local only. -Live contacts SAM and DeepSeek and prints status codes.
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$WebPort = 8001,
    [switch]$Live
)

$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$EnvFile = Join-Path $Root ".env"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$httpStatus = "unavailable"
$body = ""
try {
    $healthUrl = "http://{0}:{1}/health" -f $ListenHost, $WebPort
    $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
    $httpStatus = [string][int]$response.StatusCode
    $body = [string]$response.Content
} catch {
    $httpStatus = "unavailable"
    $body = ""
}

$mode = "local"
if ($Live) { $mode = "live" }

# PowerShell 5.1 strips quotes from an inline program, so the helper is a file.
$checker = Join-Path $PSScriptRoot "live_checks.py"

Write-Output "GovCon live checks. Values of secrets are not printed."
$body | & $Python $checker $EnvFile $mode $httpStatus
if ($LASTEXITCODE -ne 0) {
    Write-Output "The check helper failed. The error text above is from Python and should not contain a secret. Start from the repo venv if govcon could not be imported."
}
