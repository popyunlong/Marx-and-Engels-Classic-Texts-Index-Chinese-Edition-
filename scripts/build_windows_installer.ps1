$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
Set-Location $projectRoot

& (Join-Path $scriptDir "build_windows.ps1")

$latestRelease = Get-ChildItem (Join-Path $projectRoot "release") -Directory -Filter "windows-full-*" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $latestRelease) {
    throw "Full-edition release folder not found."
}

$currentLink = Join-Path $projectRoot "release\current"
if (Test-Path $currentLink) {
    Remove-Item -LiteralPath $currentLink -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $currentLink | Out-Null
Copy-Item -Path (Join-Path $latestRelease.FullName "*") -Destination $currentLink -Recurse

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    throw "ISCC.exe not found. Install Inno Setup 6 first."
}

& $iscc.Source (Join-Path $projectRoot "installer\windows_full.iss")

Write-Host ""
Write-Host "Installer ready in release\\installer"
