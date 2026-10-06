# Start the local GovCon processes against an already-running Postgres.
# Does not install Docker, does not start the Windows Postgres service, and
# does not print DATABASE_URL or any other secret.
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$PostgresPort = 5433,
    [int]$WebPort = 8001
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (Test-Path (Join-Path $PSScriptRoot "..\..\pyproject.toml")) {
    $Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

function Test-PortOpen([string]$TargetHost, [int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(2000, $false)
        if (-not $ok) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if (-not (Test-PortOpen $ListenHost $PostgresPort)) {
    Write-Error ("Postgres is not accepting connections on {0}:{1}. This script does not use Docker and does not touch a Postgres service on port 5432. Start the local Postgres 15 instance that listens on port {1}, then run this script again." -f $ListenHost, $PostgresPort)
}

$RunDir = Join-Path $Root "data\run"
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$LogDir = Join-Path $Root "logs-live"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Start-GovConProcess([string]$Name, [string[]]$ArgumentList) {
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (Test-Path $pidFile) {
        $existing = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($existing) {
            $proc = Get-Process -Id ([int]$existing) -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Output "$Name already running (pid $existing)"
                return
            }
        }
    }
    $outLog = Join-Path $LogDir "$Name.out.log"
    $errLog = Join-Path $LogDir "$Name.err.log"
    $proc = Start-Process -FilePath $Python -ArgumentList $ArgumentList -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Set-Content -Path $pidFile -Value $proc.Id
    Write-Output "$Name started (pid $($proc.Id))"
}

# Module form so this works before `pip install -e .` puts govcon.exe on PATH.
Start-GovConProcess "web" @("-m", "govcon", "web", "serve", "--host", $ListenHost, "--port", "$WebPort")
Start-GovConProcess "worker" @("-m", "govcon", "worker", "start")
# The dedicated worker runs chain and bot tasks. The scheduler only queues them.
Start-GovConProcess "scheduler" @("-m", "govcon", "scheduler", "start", "--no-worker")

Write-Output ("Web UI: http://{0}:{1}" -f $ListenHost, $WebPort)
Write-Output "Logs: $LogDir"
Write-Output "Stop with scripts\windows\Stop-GovCon.ps1"
