# Self-restarting GLM-4V OCR runner for the Marx-Engels 2nd-edition scanned volumes.
# ASCII-only (no non-ASCII literals) so Windows PowerShell 5.1 (GBK) parses it safely.
# No secret in this file: the API key is read from the USER environment variable
# ZHIPU_API_KEY. Working dir is $PSScriptRoot (this file lives in the repo root), so no
# Chinese path literal is needed. Loops the resumable OCR script until it prints the ASCII
# marker ALL_VOLUMES_DONE, restarting it if it crashes. Meant to be run by a Windows
# Scheduled Task so it survives app/session/logoff (machine sleep is separately disabled).
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
if (-not $env:ZHIPU_API_KEY) {
    $env:ZHIPU_API_KEY = [Environment]::GetEnvironmentVariable("ZHIPU_API_KEY", "User")
}
# Clean takeover: stop any OTHER OCR runners (e.g. an in-session python worker or bash loop)
# so this window is the sole runner. This wrapper is powershell.exe, so the filter never hits it.
Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -in 'python.exe','bash.exe','sh.exe') -and $_.CommandLine -like "*_ocr_quanji2_vision*" } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
Start-Sleep -Seconds 3
$log = "data\quanji2_ocr.log"
"[task-wrapper start $(Get-Date -Format s)]" | Out-File -FilePath $log -Append -Encoding utf8
for ($i = 0; $i -lt 1000; $i++) {
    if ((Test-Path $log) -and (Select-String -Path $log -SimpleMatch "ALL_VOLUMES_DONE" -Quiet -ErrorAction SilentlyContinue)) {
        "[task-wrapper done $(Get-Date -Format s)]" | Out-File -FilePath $log -Append -Encoding utf8
        break
    }
    "[task-wrapper round $i $(Get-Date -Format s)]" | Out-File -FilePath $log -Append -Encoding utf8
    & python scripts\_ocr_quanji2_vision.py --workers 6 *>> $log
    Start-Sleep -Seconds 5
}
