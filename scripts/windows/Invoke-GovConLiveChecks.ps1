# Local health plus optional live probes. Never prints a key, a database URL, or a request URL.
# Run from any directory. Default is local only. -Live contacts SAM and DeepSeek and prints status codes.
param(
    [string]$ListenHost = "127.0.0.1",
    [int]$WebPort = 8001,
    [switch]$Live
)

$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Test-NamePresent([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { $value = (Get-Item "Env:$Name" -ErrorAction SilentlyContinue).Value }
    if ([string]::IsNullOrWhiteSpace($value)) { return "absent" }
    return "present"
}

Write-Output "GovCon live checks. Values of secrets are not printed."
foreach ($Name in @("SAM_API_KEY", "DEEPSEEK_API_KEY", "JEV_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST")) {
    Write-Output ("{0}  {1}" -f $Name, (Test-NamePresent $Name))
}

$healthUrl = "http://{0}:{1}/health" -f $ListenHost, $WebPort
try {
    $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
    $payload = $response.Content | ConvertFrom-Json
    Write-Output ("GET /health  {0}  status={1}" -f [int]$response.StatusCode, $payload.status)
    if ($payload.checks) {
        foreach ($property in $payload.checks.PSObject.Properties) {
            $state = $property.Value.status
            if (-not $state) { $state = $property.Value }
            Write-Output ("  {0}  {1}" -f $property.Name, $state)
        }
    }
} catch {
    Write-Output "GET /health  unavailable. Start the web process with Start-GovCon.ps1. The error text is omitted because it can contain a URL."
}

if (-not $Live) {
    Write-Output "Live SAM and DeepSeek calls were not made. Re-run with -Live on the laptop to probe them. Output is still only a status code."
    return
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
& $Python -c @'
import os, urllib.request
def probe(name, url, headers):
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"{name}  http {resp.status}")
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(f"{name}  http {code if code is not None else 'failed'}")

sam = os.environ.get("SAM_API_KEY") or ""
if sam:
    probe("SAM", "https://api.sam.gov/prod/opportunities/v2/search?limit=1&api_key=" + sam, {})
else:
    print("SAM  skipped, key absent")
deepseek = os.environ.get("DEEPSEEK_API_KEY") or ""
if deepseek:
    probe("DeepSeek", "https://api.deepseek.com/models", {"Authorization": "Bearer " + deepseek})
else:
    print("DeepSeek  skipped, key absent")
print("JEV  not probed. A decision package can contain company data, and this script does not send one.")
'@
