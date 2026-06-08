param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun,
    [switch]$SkipRestart,
    [switch]$KeepLocalArchive,
    [switch]$RebuildCorpus
)

$ErrorActionPreference = "Stop"

Write-Host "upload_reader_patch.ps1 is now a compatibility wrapper."
Write-Host "Using unified cloud patch script: deploy/update_cloud.ps1"
Write-Host ""

$updateScript = Join-Path $PSScriptRoot "update_cloud.ps1"
if (-not (Test-Path $updateScript)) {
    throw "Missing unified cloud patch script: $updateScript"
}

$argsList = @(
    "-ExecutionPolicy", "Bypass",
    "-NoProfile",
    "-File", $updateScript,
    "-ServerHost", $ServerHost,
    "-User", $User,
    "-RemoteDir", $RemoteDir,
    "-Port", $Port,
    "-IdentityFile", $IdentityFile
)

if ($SkipRestart) {
    $argsList += "-SkipRestart"
}
if ($DryRun) {
    $argsList += "-DryRun"
}
if ($KeepLocalArchive) {
    $argsList += "-KeepLocalArchive"
}
if ($RebuildCorpus) {
    $argsList += "-RebuildCorpus"
}

& powershell @argsList
if ($LASTEXITCODE -ne 0) {
    throw "Unified cloud patch script failed."
}
