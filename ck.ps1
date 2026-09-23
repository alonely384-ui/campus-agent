# ck.ps1 - one-word health check for campus-agent
# Usage from the project folder:  .\ck.ps1
# Or from anywhere:               powershell -ExecutionPolicy Bypass -File "D:\alonely\deepseek\职业规划\ck.ps1"
# ASCII-only output.

$proj = "D:\alonely\deepseek\campus-agent"
$vpy  = "$proj\.venv\Scripts\python.exe"
$ok = 0; $bad = 0

function Chk($label, [bool]$pass, $detail = "") {
    if ($pass) { Write-Host ("  [OK]   {0} {1}" -f $label, $detail) -ForegroundColor Green; $script:ok++ }
    else       { Write-Host ("  [FAIL] {0} {1}" -f $label, $detail) -ForegroundColor Red;   $script:bad++ }
}

Write-Host ""
Write-Host "===== campus-agent health check =====" -ForegroundColor White

Write-Host ""
Write-Host "[1] tools" -ForegroundColor Cyan
Chk "git" ([bool](Get-Command git -ErrorAction SilentlyContinue)) $(if (Get-Command git -EA SilentlyContinue) { (git --version) })
Chk "project folder" (Test-Path $proj)
Chk "venv python" (Test-Path $vpy)

if (-not (Test-Path $vpy)) {
    Write-Host "`nvenv missing - re-run setup4-venv.ps1" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[2] python packages" -ForegroundColor Cyan
$mods = "fastapi","uvicorn","pydantic","pydantic_settings","openai","httpx","pytest","pytest_asyncio","ruff","chromadb","pypdf","tiktoken","redis","sqlalchemy","pymysql","multipart","sse_starlette"
$modList = ($mods | ForEach-Object { "'$_'" }) -join ','
$code = "import importlib`nmods=[$modList]`nbad=[]`nfor m in mods:`n    try: importlib.import_module(m)`n    except Exception: bad.append(m)`nprint('MISSING:'+','.join(bad) if bad else 'ALL_OK')"
$res = & $vpy -c $code 2>&1 | Out-String
if ($res -match 'ALL_OK') { Chk "all $($mods.Count) packages" $true } else { Chk "packages" $false ($res.Trim()) }

Write-Host ""
Write-Host "[3] API key" -ForegroundColor Cyan
$envFile = "$proj\.env"
if (Test-Path $envFile) {
    $c = Get-Content $envFile -Raw
    if ($c -match 'LLM_API_KEY=(sk-[A-Za-z0-9_\-]{10,})') {
        Chk ".env has a real API key" $true "($($matches[1].Length) chars)"
    } else {
        Chk ".env API key" $false "still a placeholder - run run-set-key.cmd"
    }
} else {
    Chk ".env" $false "missing - copy .env.example to .env"
}

Write-Host ""
Write-Host "[4] git" -ForegroundColor Cyan
Push-Location $proj
Chk "repo" ((git rev-parse --git-dir 2>&1) -ne $null) "$(git rev-list --count HEAD 2>&1) commits"
$remote = git remote get-url origin 2>&1
Chk "remote origin" ($LASTEXITCODE -eq 0) $remote
$dirty = (git status --porcelain 2>&1 | Out-String).Trim()
if ($dirty) { Chk "working tree" $false "has uncommitted changes" } else { Chk "working tree" $true "clean" }
Pop-Location

Write-Host ""
Write-Host "[5] tests" -ForegroundColor Cyan
Push-Location $proj
$env:PYTHONIOENCODING = "utf-8"
$t = & $vpy -m pytest -q 2>&1 | Out-String
if ($t -match '(\d+) passed') { Chk "pytest" $true "$($matches[1]) passed" } else { Chk "pytest" $false "failures - run pytest -v" }
Pop-Location

Write-Host ""
$color = if ($bad -eq 0) { 'Green' } else { 'Yellow' }
Write-Host "===== $ok ok, $bad failed =====" -ForegroundColor $color
if ($bad -eq 0) {
    Write-Host ""
    Write-Host "Everything ready. Start working:" -ForegroundColor Green
    Write-Host "  .\.venv\Scripts\python.exe -m app.cli" -ForegroundColor Gray
}
Write-Host ""
