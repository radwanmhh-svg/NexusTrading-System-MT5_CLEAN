# I-022 follow-up: restart ANY single NEXUS service by its port.
# run.py (the stack manager) supervises its service children and respawns a
# killed child with the correct venv + env automatically, so all we do is
# terminate the current process -- the fresh child then loads the latest code.
# Usage from the project root on the Nexus device:
#   powershell -NoProfile -ExecutionPolicy Bypass -File restart_nexus_service.ps1 -Port 9001
#   powershell -NoProfile -ExecutionPolicy Bypass -File restart_nexus_service.ps1 -Port 9000
param([int]$Port = 9001)
$ErrorActionPreference = 'Stop'
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen | Select-Object -First 1
if (-not $conn) { Write-Host "Nothing listening on :$Port (already down) -- run.py will respawn it"; exit 0 }
$procId = $conn.OwningProcess
Write-Host "Stopping service on :$Port PID=$procId (run.py will respawn it with fresh code)..."
taskkill /PID $procId /T /F 2>$null | Out-Null
Write-Host 'Done -- health returns in ~30-60s.'
