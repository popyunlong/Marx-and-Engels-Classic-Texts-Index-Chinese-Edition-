param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun
)

# Upload reproducible Hegel construction artifacts (MiMo page sidecars and audits).
# The searchable runtime DB is deployed separately by upload_corpus_db.ps1.
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$catalog = Join-Path $repoRoot "config\hegel_volumes.yaml"
$localDir = Join-Path $repoRoot "data\hegel"
if (-not (Test-Path -LiteralPath $catalog)) { throw "Missing catalogue: $catalog" }
if (-not (Test-Path -LiteralPath $localDir)) { throw "Missing Hegel data directory: $localDir" }

$ids = @(
    Get-Content -LiteralPath $catalog -Encoding UTF8 |
        ForEach-Object { if ($_ -match '^\s*- id:\s*([^\s#]+)') { $Matches[1] } }
)
if ($ids.Count -ne 16 -or ($ids | Sort-Object -Unique).Count -ne 16) {
    throw "Expected 16 unique volume ids in $catalog; found $($ids.Count)."
}
$sidecars = @()
foreach ($id in $ids) {
    $path = Join-Path $localDir "$id.jsonl"
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing completed sidecar: $path" }
    if ((Get-Item -LiteralPath $path).Length -eq 0) { throw "Empty sidecar: $path" }
    $sidecars += Get-Item -LiteralPath $path
}

$auditFiles = @(Get-ChildItem -LiteralPath (Join-Path $localDir "audit") -File -Filter "*.json" -ErrorAction SilentlyContinue)
$tocAudit = Join-Path $localDir "toc_audit.json"
if (Test-Path -LiteralPath $tocAudit) { $auditFiles += Get-Item -LiteralPath $tocAudit }
$files = @($sidecars + $auditFiles)

foreach ($name in @("ssh", "scp")) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { throw "Missing required command: $name" }
}
$target = "$User@$ServerHost"
$sshArgs = @("-i", $IdentityFile, "-p", $Port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
$scpArgs = @("-i", $IdentityFile, "-P", $Port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$staging = "/tmp/hegel-data-$stamp"
$remoteData = "$RemoteDir/data/hegel"

Write-Host "Hegel artifacts: sidecars=$($sidecars.Count) audits=$($auditFiles.Count)"
Write-Host "Remote target: ${target}:$remoteData"
if ($DryRun) { return }

& ssh @sshArgs $target "mkdir -p '$staging/audit' '$remoteData/audit'"
if ($LASTEXITCODE -ne 0) { throw "Could not prepare remote staging directory." }
foreach ($file in $files) {
    $remoteSubdir = if ($file.Directory.Name -eq "audit") { "$staging/audit/" } else { "$staging/" }
    & scp @scpArgs $file.FullName "${target}:$remoteSubdir"
    if ($LASTEXITCODE -ne 0) { throw "Upload failed: $($file.FullName)" }
}

$expectedBytes = [int64](($files | Measure-Object -Property Length -Sum).Sum)
$remoteCommand = @"
set -eu
count=`$(find '$staging' -maxdepth 1 -type f -name 'hegel-*.jsonl' | wc -l)
test "`$count" -eq 16
bytes=`$(find '$staging' -type f -printf '%s\n' | awk '{s+=`$1} END {print s+0}')
test "`$bytes" -eq $expectedBytes
cp -a '$remoteData' '$remoteData.bak-$stamp' 2>/dev/null || true
find '$remoteData' -maxdepth 1 -type f -name 'hegel-*.jsonl' -delete
cp -a '$staging/'*.jsonl '$remoteData/'
if find '$staging/audit' -type f -name '*.json' | grep -q .; then cp -a '$staging/audit/'*.json '$remoteData/audit/'; fi
if test -f '$staging/toc_audit.json'; then cp -a '$staging/toc_audit.json' '$remoteData/'; fi
chown -R www-data:www-data '$remoteData'
chmod -R u=rwX,go=rX '$remoteData'
rm -rf '$staging'
find '$remoteData' -maxdepth 1 -type f -name 'hegel-*.jsonl' | wc -l
"@
& ssh @sshArgs $target $remoteCommand
if ($LASTEXITCODE -ne 0) { throw "Remote Hegel artifact verification/install failed." }
Write-Host "Hegel construction artifacts uploaded and verified."
