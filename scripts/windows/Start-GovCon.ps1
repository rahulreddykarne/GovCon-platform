# Start the local GovCon processes. If TCP PostgresPort is closed, start the
# portable Postgres 15 under -PgRoot. Does not install Docker, does not start
# or stop a Postgres service on port 5432, and does not print DATABASE_URL
# or any other secret.
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$PostgresPort = 5433,
    [int]$WebPort = 8001,
    [string]$PgRoot = "C:\Users\suhas\Documents\Codex\2026-09-27\here-s-a-full-laptop-setup\work\local-postgres",
    [int]$DbConnectTimeoutSeconds = 30
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

function Get-PostmasterPid([string]$DataDir) {
    $pidFile = Join-Path $DataDir "postmaster.pid"
    if (-not (Test-Path $pidFile)) {
        return $null
    }
    $lines = @(Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue)
    if ($lines.Count -lt 1) {
        return $null
    }
    $first = [string]$lines[0]
    if ($first -match '^\s*(\d+)') {
        return [int]$Matches[1]
    }
    return $null
}

function Test-LivePostgresPid([int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $false
    }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) {
        return $false
    }
    return ($proc.ProcessName -eq "postgres")
}

function Move-StalePostmasterPid([string]$DataDir) {
    $pidFile = Join-Path $DataDir "postmaster.pid"
    if (-not (Test-Path $pidFile)) {
        return
    }
    $existing = Get-PostmasterPid $DataDir
    if (($existing -ne $null) -and (Test-LivePostgresPid $existing)) {
        return
    }
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    $moved = $pidFile + ".stale-" + $stamp
    Move-Item -LiteralPath $pidFile -Destination $moved -Force
    Write-Output "Moved stale postmaster.pid aside ($moved)"
}

function Test-PostgresSelect1 {
    # Use the app DATABASE_URL (user/password/db) via wait_postgres.py. Never
    # connect as the Windows account, and never print the URL or password.
    $helper = Join-Path $PSScriptRoot "wait_postgres.py"
    if (-not (Test-Path $helper)) {
        return $false
    }
    if ($DbConnectTimeoutSeconds -lt 1) {
        Write-Error "DbConnectTimeoutSeconds must be >= 1"
        return $false
    }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Python $helper --timeout "$DbConnectTimeoutSeconds"
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return ($code -eq 0)
}

function Wait-PostgresReady([int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ((Test-PortOpen $ListenHost $PostgresPort) -and (Test-PostgresSelect1)) {
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Start-PortablePostgres {
    if ($PostgresPort -eq 5432) {
        Write-Error "This script never starts or touches a Postgres service on port 5432."
        return
    }
    $binDir = Join-Path $PgRoot "15\bin"
    $pgCtl = Join-Path $binDir "pg_ctl.exe"
    $dataDir = Join-Path $PgRoot "data"
    $logFile = Join-Path $PgRoot "postgres-stderr.log"
    if (-not (Test-Path $pgCtl)) {
        Write-Error ("Portable Postgres pg_ctl not found at {0}. Set -PgRoot to the local-postgres folder (15\bin and data)." -f $pgCtl)
        return
    }
    if (-not (Test-Path $dataDir)) {
        Write-Error ("Portable Postgres data directory not found at {0}." -f $dataDir)
        return
    }
    $existing = Get-PostmasterPid $dataDir
    if (($existing -ne $null) -and (Test-LivePostgresPid $existing)) {
        Write-Output ("Portable Postgres already running (pid {0}); waiting for {1}:{2}" -f $existing, $ListenHost, $PostgresPort)
        return
    }
    Move-StalePostmasterPid $dataDir
    $portOpt = "-p $PostgresPort"
    Write-Output ("Starting portable Postgres 15 on {0}:{1}" -f $ListenHost, $PostgresPort)
    & $pgCtl start -D $dataDir -o $portOpt -l $logFile -w -t 600
    if ($LASTEXITCODE -ne 0) {
        Write-Error ("pg_ctl start failed with exit code {0}. See {1}." -f $LASTEXITCODE, $logFile)
    }
}

if ($PostgresPort -eq 5432) {
    Write-Error "This script never starts or touches a Postgres service on port 5432."
}

if (-not (Test-PortOpen $ListenHost $PostgresPort)) {
    Start-PortablePostgres
    if (-not (Wait-PostgresReady 600)) {
        Write-Error ("Postgres is not accepting connections on {0}:{1} after start. This script does not use Docker and does not touch a Postgres service on port 5432." -f $ListenHost, $PostgresPort)
    }
} elseif (-not (Test-PostgresSelect1)) {
    if (-not (Wait-PostgresReady 60)) {
        Write-Error ("Postgres port {0}:{1} is open but SELECT 1 failed." -f $ListenHost, $PostgresPort)
    }
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
