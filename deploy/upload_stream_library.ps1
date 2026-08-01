param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun
)

# Uploads the self-hosted "stream reading" static HTML library (stream_library/, gitignored,
# ~13MB of small text files: the web-adapted Marx-Engels Wenji) to the production server,
# OUT OF GIT, the same way static_library/ and PDFs are shipped.
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
$localDir = Join-Path $repoRoot "stream_library"
if (-not (Test-Path $localDir)) {
    throw "Missing local stream_library: $localDir. Build it first: python scripts/build_stream_wenji.py"
}

$localCount = @(Get-ChildItem -LiteralPath $localDir -Recurse -File).Count
if ($localCount -lt 1) {
    throw "stream_library has no files. Build first: python scripts/build_stream_wenji.py"
}

$remote = "$User@${ServerHost}"
# ssh needs -n (-T) so it does not read stdin; otherwise a backgrounded run hangs on the first ssh.
# scp must NOT get -n (it is not a valid scp flag), so keep a separate options array for it.
$sshOptions = @("-n", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")
$scpOptions = @("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")
if ($IdentityFile -and (Test-Path $IdentityFile)) {
    $sshOptions += @("-i", $IdentityFile)
    $scpOptions += @("-i", $IdentityFile)
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archive = Join-Path $env:TEMP "marx-stream-library-$stamp.tar.gz"
$remoteArchive = "/tmp/marx-stream-library.tar.gz"

Write-Host "Local stream_library: $localCount files"
Write-Host "Remote target: ${remote}:$RemoteDir/stream_library"

if ($DryRun) {
    Write-Host "Dry run: nothing uploaded."
    return
}

Write-Host "Creating archive ..."
Push-Location $repoRoot
try {
    & tar -czf $archive stream_library
    if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
$mb = [Math]::Round((Get-Item $archive).Length / 1MB, 2)
Write-Host "Archive ready: $mb MB"

try {
    Invoke-Remote "test -d '$RemoteDir'"
    Write-Host "Uploading archive ..."
    & scp @scpOptions -o BatchMode=yes -P $Port $archive "${remote}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }

    Write-Host "Extracting on server ..."
    Invoke-Remote "tar -xzf '$remoteArchive' -C '$RemoteDir' && rm -f '$remoteArchive'"

    Write-Host "Fixing permissions ..."
    Invoke-Remote "chown -R www-data:www-data '$RemoteDir/stream_library' && chmod -R a+rX '$RemoteDir/stream_library'"

    Write-Host "Verifying remote content (expect >= $localCount files) ..."
    Invoke-Remote "test `$(find '$RemoteDir/stream_library' -type f | wc -l) -ge $localCount && echo stream_library_verified"
} finally {
    if (Test-Path $archive) { Remove-Item -LiteralPath $archive -Force }
}

Write-Host ""
Write-Host "stream_library upload complete. Next safe step: deploy/update_cloud.ps1"
