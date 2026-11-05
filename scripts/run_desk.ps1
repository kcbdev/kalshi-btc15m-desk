# Keeps the BTC15M paper desk alive. Restarts the python engine if it exits.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null

while ($true) {
    $start = (Get-Date).ToUniversalTime().ToString("o")
    "=== supervisor: starting engine at $start" | Out-File -Append -Encoding utf8 (Join-Path $root "logs\supervisor.log")
    python -u -m kalshi15m.engine 2>&1 | Tee-Object -FilePath (Join-Path $root "logs\engine_console.log") -Append
    $code = $LASTEXITCODE
    $end = (Get-Date).ToUniversalTime().ToString("o")
    "=== supervisor: engine exited with code $code at $end; restarting in 5s" | Out-File -Append -Encoding utf8 (Join-Path $root "logs\supervisor.log")
    Start-Sleep -Seconds 5
}
