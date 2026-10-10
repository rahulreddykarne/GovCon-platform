# Stop the GovCon web, worker, and scheduler processes started by Start-GovCon.ps1.
# Does not stop Postgres.
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunDir = Join-Path $Root "data\run"

foreach ($Name in @("scheduler", "worker", "web")) {
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Output "$Name was not started by this script"
        continue
    }
    $raw = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($raw) {
        $proc = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $proc.Id -Force
            Write-Output "$Name stopped (pid $($proc.Id))"
        } else {
            Write-Output "$Name was not running (stale pid $raw)"
        }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

Write-Output "Postgres was left running."
