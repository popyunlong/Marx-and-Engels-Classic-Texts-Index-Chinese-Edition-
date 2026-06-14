param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun
)

# Uploads the self-hosted static HTML library (static_library/, gitignored, ~150MB of
# small text files) to the production server, OUT OF GIT, the same way PDFs are shipped.
# Many small files -> use one tar.gz + remote extract instead of per-file scp.
# Run this BEFORE deploy/update_cloud.ps1 so the reader has content on restart.

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Invoke-Remote {
    param([string]$Command)
    & ssh @sshOptions -p $Port $remote $Command
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed: $Command" }
}

Require-Command "ssh"
Require-Command "scp"
Require-Command "tar"

$repoRoot = Split-Path -Parent $PSScriptRoot
$localDir = Join-Path $repoRoot "static_library"
if (-not (Test-Path $localDir)) {
    throw "Missing local static_library: $localDir"
}

$localCount = @(Get-ChildItem -LiteralPath $localDir -Recurse -File).Count
if ($localCount -lt 1) {
    throw "static_library has no files. Vendor content first (scripts/vendor_static_library.py)."
}

$remote = "$User@${ServerHost}"
$sshOptions = @("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")
if ($IdentityFile -and (Test-Path $IdentityFile)) {
    $sshOptions += @("-i", $IdentityFile)
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archive = Join-Path $env:TEMP "marx-static-library-$stamp.tar.gz"
$remoteArchive = "/tmp/marx-static-library.tar.gz"

Write-Host "Local static_library: $localCount files"
Write-Host "Remote target: ${remote}:$RemoteDir/static_library"

if ($DryRun) {
    Write-Host "Dry run: nothing uploaded."
    return
}

Write-Host "Creating archive ..."
Push-Location $repoRoot
try {
    & tar -czf $archive static_library
    if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
$mb = [Math]::Round((Get-Item $archive).Length / 1MB, 2)
Write-Host "Archive ready: $mb MB"

try {
    Invoke-Remote "test -d '$RemoteDir'"
    Write-Host "Uploading archive ..."
    & scp @sshOptions -o BatchMode=yes -P $Port $archive "${remote}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }

    Write-Host "Extracting on server ..."
    Invoke-Remote "tar -xzf '$remoteArchive' -C '$RemoteDir' && rm -f '$remoteArchive'"

    Write-Host "Fixing permissions ..."
    Invoke-Remote "chown -R www-data:www-data '$RemoteDir/static_library' && chmod -R a+rX '$RemoteDir/static_library'"

    Write-Host "Verifying remote content ..."
    Invoke-Remote "c=`$(find '$RemoteDir/static_library' -type f | wc -l); echo `"remote files=`$c (local=$localCount)`"; test `"`$c`" -ge $localCount"
} finally {
    if (Test-Path $archive) { Remove-Item -LiteralPath $archive -Force }
}

Write-Host ""
Write-Host "static_library upload complete. Next safe step: deploy/update_cloud.ps1"
