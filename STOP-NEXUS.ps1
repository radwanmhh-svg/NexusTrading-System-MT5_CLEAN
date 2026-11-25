[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root '.nexus-runtime'
$pidFile = Join-Path $Runtime 'manager.pid'
$stateFile = Join-Path $Runtime 'state.json'

Write-Host '========================================' -ForegroundColor Cyan
Write-Host '       NEXUS TRADER - STOP' -ForegroundColor Cyan
Write-Host '========================================' -ForegroundColor Cyan

if (-not (Test-Path $pidFile)) {
    Write-Host 'No NEXUS stack manager PID file was found.' -ForegroundColor Yellow
    Write-Host 'This package only stops stacks started with START-NEXUS.bat.'
    Write-Host 'Manually launched processes have not been stopped.'
    exit 1
}

$pidText = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
$managerPid = 0
if (-not [int]::TryParse([string]$pidText, [ref]$managerPid)) {
    Write-Host 'The manager PID file is invalid. No processes were stopped.' -ForegroundColor Red
    exit 1
}

$managerInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$managerPid" -ErrorAction SilentlyContinue
if (-not $managerInfo -or $managerInfo.CommandLine -notlike '*nexus_stack_manager.py*') {
    Write-Host 'The recorded NEXUS manager is not running. No unrelated process will be terminated.' -ForegroundColor Yellow
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 1
}

New-Item -ItemType File -Force -Path (Join-Path $Runtime 'stop.request') | Out-Null
Write-Host "Requested managed shutdown from NEXUS manager PID $managerPid..." -ForegroundColor Yellow

$deadline = (Get-Date).AddSeconds(75)
$stopped = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $managerInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$managerPid" -ErrorAction SilentlyContinue
    $state = $null
    if (Test-Path $stateFile) {
        try { $state = Get-Content $stateFile -Raw | ConvertFrom-Json } catch { }
    }
    if (-not $managerInfo -and $state -and $state.status -eq 'stopped') {
        $stopped = $true
        break
    }
}

if (-not $stopped) {
    Write-Host 'Manager did not finish within 75 seconds. It was NOT force-killed to avoid orphaning services.' -ForegroundColor Red
    Write-Host 'Inspect logs\nexus-manager.log, logs\nexus-services.log and .nexus-runtime\manager-stderr.log.'
    exit 1
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Write-Host 'All nine NEXUS services and Dashboard manager have stopped.' -ForegroundColor Green

# I-022: the persistent tick sessions are mt5_worker.py --session child
# processes of nexus-data. They are grandchildren of the manager, so the
# managed shutdown can leave them alive holding MT5 terminal logins. Sweep
# any leftovers ONLY by their unique command line — never a blind kill.
$leftover = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*mt5_worker.py*--session*' }
if ($leftover) {
    foreach ($p in $leftover) {
        Write-Host "Stopping leftover MT5 tick session PID $($p.ProcessId)..." -ForegroundColor Yellow
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host 'No leftover MT5 tick sessions.' -ForegroundColor Green
}

# Stop only the known NEXUS Redis container. The named volume is retained.
# The script runs from a file: passing the here-string inline made wsl.exe
# strip the inner quotes, so bash died with a syntax error and the STOP
# script reported exit=1 even when the application stop had succeeded.
$bashScript = @'
set -e
# Runs under: wsl -u root ... --exec bash, so no sudo password is needed.
# If the Docker daemon is not running, the container cannot be running
# either; do not start Docker just to stop it (that also keeps this script
# working when sudo would otherwise prompt for a password).
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed; nothing to stop"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running; Redis container is already down with it"
  exit 0
fi
if docker inspect nexus-redis >/dev/null 2>&1; then
  running=$(docker inspect -f '{{.State.Running}}' nexus-redis)
  if [ "$running" = "true" ]; then docker stop nexus-redis; else echo "Redis container already stopped"; fi
else
  echo "Redis container not found; no data was removed"
fi
'@
$bashScript = $bashScript.Replace("`r`n", "`n")
$redisScriptWin = Join-Path $Runtime 'redis-stop.sh'
[IO.File]::WriteAllText($redisScriptWin, $bashScript)
$drive = $Root.Substring(0, 1).ToLowerInvariant()
$redisScriptLinux = '/mnt/' + $drive + $redisScriptWin.Substring(2).Replace('\', '/')
Write-Host "`nStopping the NEXUS Redis container (persistent volume is retained)..." -ForegroundColor Cyan
& wsl.exe -d Ubuntu-24.04 -u root --exec bash $redisScriptLinux
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Application processes stopped, but the Redis stop command reported an issue. Check WSL output.' -ForegroundColor Yellow
    exit 1
}

Write-Host "`nNEXUS stopped. Redis AOF/RDB and Docker volume were not deleted." -ForegroundColor Green
Write-Host 'To start again, double-click START-NEXUS.bat.' -ForegroundColor Green
