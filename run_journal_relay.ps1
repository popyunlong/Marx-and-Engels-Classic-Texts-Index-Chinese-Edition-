# Journal relay push wrapper for Windows Task Scheduler.
# ASCII-only on purpose (PS 5.1 + GBK parsing pitfall). Scheduled daily.
# Collects Chinese journal sources from this (China-network) machine and
# pushes journal_relay.json to the production server over scp.
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot
$logDir = Join-Path $env:LOCALAPPDATA 'marx-journal-relay'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir 'journal_relay.log'
# Keep the log from growing without bound: rotate at ~1 MB.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    Move-Item -Force $log "$log.1"
}
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "=== $stamp start"
cmd /c "python scripts\journal_relay_push.py >> `"$log`" 2>&1"
Add-Content -Path $log -Value "=== $stamp exit=$LASTEXITCODE"
exit $LASTEXITCODE
