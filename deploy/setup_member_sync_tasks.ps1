#requires -Version 5.1
<#
  setup_member_sync_tasks.ps1
  Register two Windows Scheduled Tasks that run sync_members_local.ps1 on this PC:
    MarxMemberSync-Incremental : every 1 minute  -> -Mode Incremental (near-instant capture
                                 of each member payment via the append-only ledger)
    MarxMemberSync-Full        : daily at 23:55   -> -Mode Full (whole roster + readable JSON)

  Tasks run as the CURRENT user (the account that owns the SSH key under ~/.ssh), so they
  fire whenever you are logged on; StartWhenAvailable catches up a missed daily run.

  Usage:
    powershell -ExecutionPolicy Bypass -File deploy\setup_member_sync_tasks.ps1
    powershell -ExecutionPolicy Bypass -File deploy\setup_member_sync_tasks.ps1 -Unregister
#>
param(
    [string]$LocalDir = "$env:USERPROFILE\marx-member-backups",
    [string]$DailyTime = "23:55",
    [switch]$SkipInitialSync,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$fullTask = "MarxMemberSync-Full"
$incTask = "MarxMemberSync-Incremental"
$syncScript = Join-Path $PSScriptRoot "sync_members_local.ps1"

function Remove-TaskIfPresent {
    param([string]$Name)
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed existing task: $Name"
    }
}

if ($Unregister) {
    Remove-TaskIfPresent $fullTask
    Remove-TaskIfPresent $incTask
    Write-Host "Unregistered member-sync tasks."
    return
}

if (-not (Test-Path $syncScript)) { throw "Cannot find sync script: $syncScript" }

$commonArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -LocalDir "{1}"' -f $syncScript, $LocalDir

# --- Incremental: every 1 minute, indefinitely ---
$incAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($commonArgs + " -Mode Incremental")
$incTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$incSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -DontStopOnIdleEnd
Register-ScheduledTask -TaskName $incTask -Action $incAction -Trigger $incTrigger -Settings $incSettings -Description "Marx Search: pull member payment ledger to this PC every minute (disaster recovery)." -Force | Out-Null
Write-Host "Registered task: $incTask (every 1 minute)"

# --- Full: daily ---
$fullAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($commonArgs + " -Mode Full")
$fullTrigger = New-ScheduledTaskTrigger -Daily -At $DailyTime
$fullSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $fullTask -Action $fullAction -Trigger $fullTrigger -Settings $fullSettings -Description "Marx Search: daily full membership backup to this PC (disaster recovery)." -Force | Out-Null
Write-Host "Registered task: $fullTask (daily at $DailyTime)"

if (-not $SkipInitialSync) {
    Write-Host "Running an initial Full sync to verify connectivity and seed the local backup ..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $syncScript -LocalDir $LocalDir -Mode Full
}

Write-Host ""
Write-Host "Done. Local backups: $LocalDir"
Write-Host "Manage: Get-ScheduledTask MarxMemberSync-*   |   remove: -Unregister"
