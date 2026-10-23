# One-click public demo: expose the local FastAPI app through a Cloudflare quick tunnel.
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\serve_public.ps1
#
# What it does:
#   1. If nothing listens on 8000, start uvicorn in the background (log: _public\server.log)
#   2. Start a cloudflared quick tunnel and print the public URL (also saved to _public\public-url.txt)
#   3. Wait until Ctrl+C; the tunnel process is a child, kill it to tear down
#
# Note: quick-tunnel hostnames are random and change on every restart. They are fine for
# demos, not for the long-lived URL in the submission. For a stable URL use a named tunnel
# (needs a domain) or deploy to Render/Railway.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
if (-not (Test-Path $cloudflared)) {
    throw "cloudflared not found at $cloudflared. Install: winget install --id Cloudflare.cloudflared -e"
}

$publicDir = Join-Path $root '_public'
New-Item -ItemType Directory -Force $publicDir | Out-Null

# 1. local server
$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Host 'No server on 8000, starting uvicorn ...'
    Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--log-level', 'warning' `
        -RedirectStandardOutput (Join-Path $publicDir 'server.log') `
        -RedirectStandardError (Join-Path $publicDir 'server.err.log') `
        -WindowStyle Hidden
    Start-Sleep -Seconds 8
}

$health = $null
try { $health = Invoke-WebRequest 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 10 } catch { }
if (-not $health -or $health.StatusCode -ne 200) {
    throw 'Local server is not healthy (/health != 200). Check _public\server.err.log'
}
Write-Host "Local server OK: $($health.Content)"

# 2. tunnel
Write-Host 'Starting cloudflared quick tunnel (about 5-10s) ...'
$tunnelLog = Join-Path $publicDir 'tunnel.log'
$tunnelErrLog = Join-Path $publicDir 'tunnel.err.log'
if (Test-Path $tunnelLog) { Remove-Item $tunnelLog -Force }
if (Test-Path $tunnelErrLog) { Remove-Item $tunnelErrLog -Force }
$tunnel = Start-Process -FilePath $cloudflared `
    -ArgumentList 'tunnel', '--url', 'http://127.0.0.1:8000', '--no-autoupdate' `
    -RedirectStandardOutput $tunnelLog -RedirectStandardError $tunnelErrLog -WindowStyle Hidden -PassThru

$url = $null
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    foreach ($candidate in @($tunnelLog, $tunnelErrLog)) {
        if (Test-Path $candidate) {
            $m = Select-String -Path $candidate -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value; break }
        }
    }
    if ($url) { break }
}
if (-not $url) { throw "Tunnel gave no URL. See $tunnelLog and $tunnelErrLog" }

$url | Set-Content (Join-Path $publicDir 'public-url.txt') -Encoding UTF8
Write-Host ''
Write-Host "PUBLIC URL: $url" -ForegroundColor Green
Write-Host "UI:         $url/ui/"
Write-Host "Docs:       $url/docs"
Write-Host "Saved to _public\public-url.txt. Press Ctrl+C to stop the tunnel."
Write-Host ''

Wait-Process -Id $tunnel.Id
