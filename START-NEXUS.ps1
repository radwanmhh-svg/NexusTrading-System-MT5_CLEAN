[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root '.nexus-runtime'
$Logs = Join-Path $Root 'logs'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$ManagerScript = Join-Path $Root 'nexus_stack_manager.py'
$EnvFile = Join-Path $Root '.env'
$DashboardBuild = Join-Path $Root 'dashboard\.next\BUILD_ID'
$BootstrapKeepAlive = $null

function Stop-WithMessage([string]$Message) {
    if ($BootstrapKeepAlive -and -not $BootstrapKeepAlive.HasExited) {
        Stop-Process -Id $BootstrapKeepAlive.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "`nSTART-NEXUS: $Message" -ForegroundColor Red
    exit 1
}

Write-Host '========================================' -ForegroundColor Cyan
Write-Host '       NEXUS TRADER - START' -ForegroundColor Cyan
Write-Host '========================================' -ForegroundColor Cyan

foreach ($path in @($Python, $ManagerScript, $EnvFile, $DashboardBuild, (Join-Path $Root 'run.py'))) {
    if (-not (Test-Path $path)) { Stop-WithMessage "Required file not found: $path" }
}

$modeLine = Get-Content $EnvFile | Where-Object { $_ -match '^\s*NEXUS_TRADING_MODE\s*=' } | Select-Object -Last 1
if (-not $modeLine -or $modeLine -notmatch '^\s*NEXUS_TRADING_MODE\s*=\s*paper\s*(#.*)?$') {
    Stop-WithMessage 'The .env file must explicitly contain NEXUS_TRADING_MODE=paper. No services were started.'
}

New-Item -ItemType Directory -Force -Path $Runtime, $Logs | Out-Null

$pidFile = Join-Path $Runtime 'manager.pid'
if (Test-Path $pidFile) {
    $oldPidText = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $oldPid = 0
    if ([int]::TryParse([string]$oldPidText, [ref]$oldPid)) {
        $oldProc = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction SilentlyContinue
        if ($oldProc -and $oldProc.CommandLine -like '*nexus_stack_manager.py*') {
            Stop-WithMessage "NEXUS manager already appears to be running (PID $oldPid). Use STOP-NEXUS.ps1 first."
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# Redis is intentionally NOT treated as a port conflict: the managed Redis
# container may already be running with restart-policy=unless-stopped. The
# script verifies that it is Redis 7 before launching application services.
# I-022: also sweep leftover MT5 tick sessions (mt5_worker.py --session) from
# a previous run — they hold MT5 terminal logins and would linger alongside
# the fresh nexus-data sessions. Matched strictly by command line.
$leftoverSessions = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*mt5_worker.py*--session*' }
foreach ($p in $leftoverSessions) {
    Write-Host "Stopping leftover MT5 tick session PID $($p.ProcessId)..." -ForegroundColor Yellow
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

$ports = @(3100, 9000, 9001, 9002, 9003, 9004, 9005, 9006, 9007, 9008)
$occupied = @()
foreach ($port in $ports) {
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $occupied += [pscustomobject]@{ Port = $port; PID = $listener.OwningProcess; Process = $proc.ProcessName }
    }
}
if ($occupied.Count -gt 0) {
    Write-Host 'These NEXUS application ports are already occupied:' -ForegroundColor Yellow
    $occupied | Format-Table -AutoSize
    Stop-WithMessage 'To avoid duplicate services, stop manually launched NEXUS service and Dashboard terminals first. Redis on 6379 may remain running.'
}

# Keep WSL alive during the hand-off from the bootstrap command to the manager.
# The Python manager starts its own keepalive before this temporary one is released.
Write-Host "`nStarting Ubuntu-24.04 WSL keepalive..." -ForegroundColor Cyan
try {
    $BootstrapKeepAlive = Start-Process -FilePath 'wsl.exe' -ArgumentList '-d Ubuntu-24.04 --exec sleep infinity' -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 2
} catch {
    Stop-WithMessage "Could not start WSL keepalive: $($_.Exception.Message)"
}

$drive = $Root.Substring(0, 1).ToLowerInvariant()
$linuxRoot = '/mnt/' + $drive + $Root.Substring(2).Replace('\', '/')
$bashScript = @'
set -e
# This script runs under: wsl -u root ... --exec bash
# so no interactive sudo password prompt is needed.
# Native Redis inside WSL (outside Docker) is supported: if it already
# answers PONG, skip the Docker path entirely.
if redis-cli -h 127.0.0.1 ping 2>/dev/null | grep -q PONG; then
  echo 'PONG (native redis already running)'
  exit 0
fi
# Start Docker. An unclean shutdown can leave a corrupt network store that
# makes the daemon crash-loop with "networks have same bridge name"; in that
# case back up and remove local-kv.db and retry. Only network metadata is
# touched: containers, images and volumes are kept.
systemctl start docker || true
for i in $(seq 1 15); do
  if docker info >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo 'Docker daemon failed to start; repairing the network store...' >&2
  systemctl stop docker || true
  KV=/var/lib/docker/network/files/local-kv.db
  if [ -f "$KV" ]; then
    cp -a "$KV" "$KV.bak-$(date +%Y%m%d-%H%M%S)"
    rm -f "$KV"
  fi
  systemctl start docker || true
  for i in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if ! docker info >/dev/null 2>&1; then
    echo 'Docker daemon is still not running after the network-store repair.' >&2
    exit 1
  fi
fi
cd '__LINUX_ROOT__'
if docker inspect nexus-redis >/dev/null 2>&1; then
  docker update --restart unless-stopped nexus-redis >/dev/null || true
  running=$(docker inspect -f '{{.State.Running}}' nexus-redis || echo false)
  if [ "$running" != "true" ]; then docker start nexus-redis >/dev/null || true; fi
else
  docker compose up -d redis
  docker update --restart unless-stopped nexus-redis >/dev/null || true
fi
# A container can be "Up" yet not publish 6379 (its network was lost during
# a repair), which an in-container ping alone cannot detect. Require the
# HOST-side mapping too, and recreate the container (named volume is kept)
# when the port is missing.
if ! docker port nexus-redis 6379 2>/dev/null | grep -q 6379; then
  echo 'nexus-redis does not publish host port 6379; recreating the container...' >&2
  docker compose up -d --force-recreate redis
fi
# docker start/compose return before Redis finishes loading its AOF (a cold
# load after boot takes ~15s), so wait for readiness instead of racing it.
for i in $(seq 1 30); do
  if out=$(docker exec nexus-redis redis-cli ping 2>/dev/null) && [ "$out" = "PONG" ] \
     && docker port nexus-redis 6379 2>/dev/null | grep -q 6379; then
    echo "$out"
    exit 0
  fi
  sleep 1
done
echo 'Redis did not answer PONG with host port 6379 published within 30 seconds' >&2
exit 1
'@
# The bootstrap now runs from a file: wsl.exe re-quotes multi-line arguments
# passed inline and strips the inner double quotes, which used to reach bash
# as "[: !=: unary operator expected" and hid real failures.
$bashScript = $bashScript.Replace('__LINUX_ROOT__', $linuxRoot).Replace("`r`n", "`n")
$redisScriptWin = Join-Path $Runtime 'redis-start.sh'
[IO.File]::WriteAllText($redisScriptWin, $bashScript)
$redisScriptLinux = '/mnt/' + $drive + $redisScriptWin.Substring(2).Replace('\', '/')
Write-Host "`nStarting Docker and Redis in Ubuntu-24.04..." -ForegroundColor Cyan
# -u root: the script above runs as root, so no interactive sudo password
# is ever required (sudo inside WSL would otherwise prompt and hang the run).
& wsl.exe -d Ubuntu-24.04 -u root --exec bash $redisScriptLinux
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Could not start Redis in Ubuntu-24.04. Check the WSL/Docker output above.' }

# Windows reaches Redis through a netsh portproxy rule
# (127.0.0.1:6379 -> <WSL eth0 IP>:6379). WSL's IP changes whenever the
# distro or its virtual network restarts, which silently breaks that rule.
# Only rebuilt when the connectivity check below actually fails. netsh
# mutations go through cmd.exe so their stderr never raises a terminating
# error under $ErrorActionPreference='Stop'; a non-elevated shell simply
# fails the rebuild and the check reports the real problem.
function Repair-RedisPortProxy {
    try {
        $wslIpRaw = (& wsl.exe -d Ubuntu-24.04 -u root --exec bash -c "ip -4 -o addr show dev eth0" | Out-String)
        $wslIpMatch = [regex]::Match($wslIpRaw, 'inet\s+(\d+\.\d+\.\d+\.\d+)')
        if (-not $wslIpMatch.Success) {
            Write-Host 'Could not read the WSL eth0 IPv4 address; leaving the portproxy rule untouched.' -ForegroundColor Yellow
            return $false
        }
        $wslIp = $wslIpMatch.Groups[1].Value
        Write-Host "Refreshing the 6379 portproxy rule -> $wslIp ..." -ForegroundColor Yellow
        cmd /c "netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=6379 >nul 2>&1"
        cmd /c "netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=6379 >nul 2>&1"
        cmd /c "netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=6379 connectaddress=$wslIp connectport=6379 >nul 2>&1"
        $check = (netsh interface portproxy show v4tov4 listenport=6379 | Out-String)
        if ($check -match [regex]::Escape($wslIp)) { return $true }
        Write-Host 'portproxy refresh was refused; run START-NEXUS from an elevated (Administrator) shell if this keeps failing.' -ForegroundColor Yellow
        return $false
    } catch {
        return $false
    }
}

Write-Host "`nChecking Redis from the Windows Python environment..." -ForegroundColor Cyan
$redisCheck = "import redis; r=redis.Redis.from_url('redis://127.0.0.1:6379', socket_connect_timeout=5, socket_timeout=5); print('REDIS_PING:', r.ping()); v=r.info('server')['redis_version']; print('REDIS_VERSION:', v); assert str(v).split('.')[0] == '7', 'Expected Redis 7'"
& $Python -c $redisCheck
if ($LASTEXITCODE -ne 0) {
    # Redis answered PONG inside WSL but Windows cannot reach it: the usual
    # cause is a stale portproxy target after the WSL IP changed.
    Write-Host '127.0.0.1:6379 did not answer from Windows; attempting to refresh the portproxy rule...' -ForegroundColor Yellow
    Repair-RedisPortProxy | Out-Null
    & $Python -c $redisCheck
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Windows Python could not verify Redis 7 at 127.0.0.1:6379 (WSL portproxy refresh did not help). NEXUS services were not started.' }
}

$managerStdout = Join-Path $Runtime 'manager-stdout.log'
$managerStderr = Join-Path $Runtime 'manager-stderr.log'
# A state.json left over from the previous run would be picked up by the
# wait loop below before the new manager publishes its own state, making
# the old status look like this run's result. Keep it as state.prev.json
# for diagnostics and start from a clean slate.
$stateFile = Join-Path $Runtime 'state.json'
if (Test-Path $stateFile) {
    Move-Item -Path $stateFile -Destination (Join-Path $Runtime 'state.prev.json') -Force -ErrorAction SilentlyContinue
}
$arguments = '"' + $ManagerScript + '" serve'
try {
    $manager = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $managerStdout -RedirectStandardError $managerStderr
    Set-Content -Path $pidFile -Value $manager.Id -Encoding ASCII
} catch {
    Stop-WithMessage "Could not start NEXUS manager: $($_.Exception.Message)"
}

# The manager has now had time to start its own WSL keepalive.
Start-Sleep -Seconds 3
if ($BootstrapKeepAlive -and -not $BootstrapKeepAlive.HasExited) {
    Stop-Process -Id $BootstrapKeepAlive.Id -Force -ErrorAction SilentlyContinue
    $BootstrapKeepAlive = $null
}

Write-Host "`nNEXUS manager started (PID $($manager.Id)). Waiting for all nine NEXUS services and Dashboard..." -ForegroundColor Cyan
# The manager now waits up to 360s for slow services (nexus-agents) plus up
# to 75s for the dashboard; allow that whole window before declaring failure.
$deadline = (Get-Date).AddSeconds(480)
$stateFile = Join-Path $Runtime 'state.json'
$finalState = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $p = Get-Process -Id $manager.Id -ErrorAction SilentlyContinue
    if (-not $p) { break }
    if (Test-Path $stateFile) {
        try { $finalState = Get-Content $stateFile -Raw | ConvertFrom-Json } catch { }
        if ($finalState -and $finalState.status -in @('running', 'degraded', 'error')) { break }
    }
}

if (-not $finalState) {
    Write-Host 'Manager has not published a final startup state yet. Check logs in .nexus-runtime and logs.' -ForegroundColor Yellow
    exit 1
}

# The manager's live recheck promotes services that were still loading when
# the first snapshot was written (every 30s). Give it a bounded grace period
# so this report reflects the stack as it is now, not the first measurement.
if ($finalState.status -eq 'degraded') {
    Write-Host 'Some services were still starting at first measurement; watching for up to 150 more seconds...' -ForegroundColor Yellow
    $graceDeadline = (Get-Date).AddSeconds(150)
    while ((Get-Date) -lt $graceDeadline) {
        Start-Sleep -Seconds 5
        try { $newState = Get-Content $stateFile -Raw | ConvertFrom-Json } catch { $newState = $null }
        if ($newState -and $newState.status -in @('running', 'error')) { $finalState = $newState; break }
        if (-not (Get-Process -Id $manager.Id -ErrorAction SilentlyContinue)) { break }
    }
}

$stateColor = if ($finalState.status -eq 'running') { 'Green' } else { 'Yellow' }
Write-Host "`nNEXUS startup state: $($finalState.status)" -ForegroundColor $stateColor
if ($finalState.services) { $finalState.services | Format-List }
Write-Host 'Dashboard: http://127.0.0.1:3100'
Write-Host 'API health: http://127.0.0.1:9000/health'
Write-Host 'ML health: http://127.0.0.1:9003/health'
Write-Host 'Data health: http://127.0.0.1:9001/health'
Write-Host 'Features health: http://127.0.0.1:9002/health'
Write-Host 'ML health: http://127.0.0.1:9003/health'
Write-Host 'Agents health: http://127.0.0.1:9004/health'
Write-Host 'News health: http://127.0.0.1:9005/health'
Write-Host 'Risk health: http://127.0.0.1:9006/health'
Write-Host 'Executor health: http://127.0.0.1:9007/health'
Write-Host 'Monitor health: http://127.0.0.1:9008/health'
Write-Host "Logs: $Logs"
Write-Host "`nTrading mode is PAPER. Use STOP-NEXUS.bat for a managed shutdown." -ForegroundColor Green
try { Start-Process 'http://127.0.0.1:3100' } catch { Write-Host 'Open http://127.0.0.1:3100 in your browser.' -ForegroundColor Yellow }
