#requires -Version 5.1
<#
  setup_member_sync_tasks.ps1
  Register two Windows Scheduled Tasks that run sync_members_local.ps1 on this PC:
    MarxMemberSync-Incremental : every 1 minute -> -Mode Incremental (near-instant capture of
                                 each member payment via the append-only ledger)
    MarxMemberSync-Full        : daily          -> -Mode Full (whole roster + readable JSON)

  The tasks use an S4U principal (LogonType S4U): they run in the non-interactive background
  session, so NO console window ever appears -- and they run whether or not you are logged on.
  Key-based SSH still works (it reads the local key file; no network password is needed).

  Registering an S4U task requires Administrator, so RUN THIS IN AN ELEVATED PowerShell
  ("Run as administrator"). The script refuses to run un-elevated rather than fall back to a
  task that flashes a console window every minute.

  Usage (elevated):
    powershell -ExecutionPolicy Bypass -File deploy\setup_member_sync_tasks.ps1
    powershell -ExecutionPolicy Bypass -File deploy\setup_member_sync_tasks.ps1 -Unregister
#>
param(
    [string]$LocalDir = "$env:USERPROFILE\marx-member-backups",
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [string]$DailyTime = "23:55",
    [switch]$SkipInitialSync,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$fullTask = "MarxMemberSync-Full"
$incTask = "MarxMemberSync-Incremental"
$syncScript = Join-Path $PSScriptRoot "sync_members_local.ps1"

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Remove-TaskIfPresent {
    param([string]$Name)
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed task: $Name"
    }
}

if ($Unregister) {
    Remove-TaskIfPresent $fullTask
    Remove-TaskIfPresent $incTask
    Write-Host "Unregistered member-sync tasks."
    return
}

if (-not (Test-Path $syncScript)) { throw "Cannot find sync script: $syncScript" }

if (-not (Test-IsAdmin)) {
    Write-Warning "Registering hidden (S4U) tasks requires Administrator."
    Write-Host "Open PowerShell as administrator and run:"
    Write-Host ""
    Write-Host ("    powershell -ExecutionPolicy Bypass -File `"{0}`"" -f $PSCommandPath)
    Write-Host ""
    throw "Not elevated; aborting so as not to create a window-flashing task."
}

# S4U principal -> background session, no console window, no stored password.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$psArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -LocalDir "{1}" -IdentityFile "{2}"' -f $syncScript, $LocalDir, $IdentityFile

# Recreate cleanly (deleting a running task before re-adding avoids in-place-update RPC errors).
Remove-TaskIfPresent $incTask
Remove-TaskIfPresent $fullTask

# --- Incremental: every 1 minute, indefinitely ---
$incAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($psArgs + " -Mode Incremental")
$incTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$incSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -DontStopOnIdleEnd
Register-ScheduledTask -TaskName $incTask -Action $incAction -Trigger $incTrigger -Settings $incSettings -Principal $principal -Description "Marx Search: pull member payment ledger to this PC every minute (disaster recovery, hidden)." | Out-Null
Write-Host "Registered: $incTask (every 1 minute, hidden via S4U)"

# --- Full: daily ---
$fullAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($psArgs + " -Mode Full")
$fullTrigger = New-ScheduledTaskTrigger -Daily -At $DailyTime
$fullSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $fullTask -Action $fullAction -Trigger $fullTrigger -Settings $fullSettings -Principal $principal -Description "Marx Search: daily full membership backup to this PC (disaster recovery, hidden)." | Out-Null
Write-Host "Registered: $fullTask (daily at $DailyTime, hidden via S4U)"

if (-not $SkipInitialSync) {
    Write-Host "Running an initial Full sync to verify connectivity and seed the local backup ..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $syncScript -LocalDir $LocalDir -IdentityFile $IdentityFile -Mode Full
}

Write-Host ""
Write-Host "Done. Local backups: $LocalDir"
Write-Host "Verify hidden:  Get-ScheduledTask MarxMemberSync-* | % { `$_.TaskName + ' ' + `$_.Principal.LogonType }"
Write-Host "Remove:         powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Unregister"
