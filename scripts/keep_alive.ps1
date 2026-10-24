# keep_alive.ps1 - keep uvicorn + cloudflared alive unattended (ASCII only)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\keep_alive.ps1
$ErrorActionPreference = "SilentlyContinue"
$base = Split-Path -Parent $PSScriptRoot
Set-Location $base
New-Item -ItemType Directory -Force "$base\_public" | Out-Null
New-Item -ItemType Directory -Force "$base\logs" | Out-Null
$python = "$base\.venv\Scripts\python.exe"
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$urlFile = "$base\_public\public-url.txt"
$log = "$base\logs\keep_alive.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $log -Value $line -Encoding UTF8
    Write-Output $line
}

function PortUp {
    return [bool](Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
}

function FastAlive {
    return [bool](Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue)
}

Log "supervisor start (pid $PID)"

while ($true) {
    if (-not (PortUp)) {
        Log "uvicorn down -> restart"
        Remove-Item Env:http_proxy,Env:https_proxy,Env:no_proxy,Env:all_proxy -ErrorAction SilentlyContinue
        Start-Process -FilePath $python `
            -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000","--log-level","warning" `
            -WorkingDirectory $base -WindowStyle Hidden `
            -RedirectStandardOutput "$base\logs\uvicorn.log" -RedirectStandardError "$base\logs\uvicorn.err.log"
        Start-Sleep -Seconds 8
        if (PortUp) { Log "uvicorn back up" } else { Log "uvicorn still down" }
    }

    if (-not (FastAlive)) {
        Log "cloudflared down -> restart"
        Remove-Item Env:http_proxy,Env:https_proxy,Env:no_proxy,Env:all_proxy -ErrorAction SilentlyContinue
        Start-Process -FilePath $cloudflared `
            -ArgumentList "tunnel","--url","http://127.0.0.1:8000","--no-autoupdate" `
            -WorkingDirectory $base -WindowStyle Hidden `
            -RedirectStandardOutput "$base\logs\cloudflared.log" -RedirectStandardError "$base\logs\cloudflared.err.log"
        Start-Sleep -Seconds 12
    }

    if (Test-Path "$base\logs\cloudflared.log") {
        $hit = Select-String -Path "$base\logs\cloudflared.log" -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
            Select-Object -Last 1
        if ($hit) {
            $u = [regex]::Match($hit.Line, 'https://[a-z0-9-]+\.trycloudflare\.com').Value
            if ($u -and ((Get-Content $urlFile -ErrorAction SilentlyContinue) -ne $u)) {
                Set-Content -Path $urlFile -Value $u -Encoding UTF8
                Log "public url = $u"
            }
        }
    }
    Start-Sleep -Seconds 60
}
