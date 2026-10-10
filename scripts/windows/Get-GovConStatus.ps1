# Show whether the local GovCon processes and Postgres port are up.
# Does not print DATABASE_URL or other secrets.
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$PostgresPort = 5433,
    [int]$WebPort = 8001
)

$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunDir = Join-Path $Root "data\run"

function Test-PortOpen([string]$TargetHost, [int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(1500, $false)
        if (-not $ok) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

$pg = Test-PortOpen $ListenHost $PostgresPort
$web = Test-PortOpen $ListenHost $WebPort
Write-Output ("Postgres {0}:{1}  {2}" -f $ListenHost, $PostgresPort, $(if ($pg) {"listening"} else {"not listening"}))
Write-Output ("Web      {0}:{1}  {2}" -f $ListenHost, $WebPort, $(if ($web) {"listening"} else {"not listening"}))

foreach ($Name in @("web", "worker", "scheduler")) {
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Output "$Name  no pid file"
        continue
    }
    $raw = Get-Content $pidFile -ErrorAction SilentlyContinue
    $proc = $null
    if ($raw) { $proc = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue }
    if ($proc) {
        Write-Output "$Name  running pid $($proc.Id)"
    } else {
        Write-Output "$Name  pid file present but process is not running"
    }
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
Write-Output "--- govcon status ---"
& $Python -m govcon status
