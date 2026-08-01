# One-time setup: install a Windows Scheduled Task that runs the 全集二版 OCR unattended,
# surviving app/session close, logoff, and reboot (machine sleep is disabled separately).
# ASCII-only. No secret stored in this file: you pass the key as -ApiKey; it is saved to your
# USER environment variable ZHIPU_API_KEY (which the keyless wrapper run_quanji2_ocr.ps1 reads).
#
# Run from a normal (non-admin) PowerShell window:
#   cd "D:\claudecode文件夹\【增强】马恩《文集》《全集》检索"
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_quanji2_task.ps1 -ApiKey "YOUR_GLM_KEY"
#
# To stop/remove later:  Unregister-ScheduledTask -TaskName Quanji2OCR -Confirm:$false
param([Parameter(Mandatory=$true)][string]$ApiKey)
$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot

# 1) stop any in-session OCR already running (python worker AND the bash self-restart loop),
#    so the scheduled task is the only runner — otherwise the bash loop would relaunch python.
Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -in 'python.exe','bash.exe','sh.exe') -and $_.CommandLine -like "*_ocr_quanji2_vision*" } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
Start-Sleep -Seconds 3

# 2) persist the key as a user env var (only YOU run this, so it is your decision)
[Environment]::SetEnvironmentVariable("ZHIPU_API_KEY", $ApiKey, "User")
$env:ZHIPU_API_KEY = $ApiKey

# 3) register + start the scheduled task running the keyless wrapper
$ps1 = Join-Path $repo "run_quanji2_ocr.ps1"
if (-not (Test-Path $ps1)) { throw "Missing wrapper: $ps1" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 23) -MultipleInstances IgnoreNew `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "Quanji2OCR" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "Quanji2OCR"
Start-Sleep -Seconds 10

$state = (Get-ScheduledTask -TaskName "Quanji2OCR").State
$alive = [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*_ocr_quanji2_vision*" })
Write-Host "Scheduled task 'Quanji2OCR' state=$state  OCR python running=$alive"
Write-Host "Done. It now runs unattended; you can close the Claude Code app. Progress logs to data\quanji2_ocr.log"
