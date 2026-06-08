param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun
)

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
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed: $Command"
    }
}

function Invoke-Remote-Test {
    param([string]$Command)
    & ssh @sshOptions -p $Port $remote $Command | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-UploadFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LocalPath,
        [Parameter(Mandatory = $true)]
        [string]$RemoteTarget
    )

    $attempts = @(
        @{ Label = "default"; ExtraArgs = @() },
        @{ Label = "legacy"; ExtraArgs = @("-O") }
    )
    foreach ($attempt in $attempts) {
        $args = @()
        $args += $attempt.ExtraArgs
        $args += $sshOptions
        $args += @("-o", "BatchMode=yes", "-P", $Port, $LocalPath, $RemoteTarget)
        Write-Host "  -> scp mode: $($attempt.Label)"
        & scp @args
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Write-Warning "scp $($attempt.Label) mode failed for $LocalPath"
    }
    throw "Failed to upload $LocalPath with both default and legacy scp modes."
}

Require-Command "ssh"
Require-Command "scp"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pdfRoot = Join-Path $repoRoot "pdfs"
if (-not (Test-Path $pdfRoot)) {
    throw "Missing local PDF root: $pdfRoot"
}

$candidateDirs = @(
    Get-ChildItem -LiteralPath $pdfRoot -Directory |
        Where-Object { @(Get-ChildItem -LiteralPath $_.FullName -Filter "*.pdf" -File).Count -eq 60 } |
        Sort-Object Name
)
if ($candidateDirs.Count -ne 1) {
    throw "Expected exactly one local PDF directory with 60 files, found $($candidateDirs.Count). Refusing to upload."
}

$localDir = $candidateDirs[0].FullName
$remoteLeaf = $candidateDirs[0].Name
$pdfs = @(Get-ChildItem -LiteralPath $localDir -Filter "*.pdf" -File | Sort-Object Name)
if ($pdfs.Count -ne 60) {
    throw "Expected 60 Lenin PDFs, found $($pdfs.Count). Refusing to upload."
}

$remote = "$User@${ServerHost}"
$remotePdfDir = "$RemoteDir/pdfs/$remoteLeaf"
$sshOptions = @("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")
if ($IdentityFile -and (Test-Path $IdentityFile)) {
    $sshOptions += @("-i", $IdentityFile)
}

$totalBytes = ($pdfs | Measure-Object -Property Length -Sum).Sum
$totalGb = [Math]::Round($totalBytes / 1GB, 2)
Write-Host "Lenin PDF upload plan: $($pdfs.Count) files, $totalGb GB"
Write-Host "Remote target: ${remote}:$remotePdfDir"

if ($DryRun) {
    Write-Host "Dry run: no cloud connection or upload will be made."
    return
}

Write-Host "Checking remote project and preparing Lenin PDF directory ..."
Invoke-Remote "test -d '$RemoteDir' && test -d '$RemoteDir/pdfs' && mkdir -p '$remotePdfDir'"

$uploaded = 0
$skipped = 0
foreach ($pdf in $pdfs) {
    $remoteFile = "$remotePdfDir/$($pdf.Name)"
    $size = [int64]$pdf.Length
    $existsSameSize = Invoke-Remote-Test "test -f '$remoteFile' && test -s '$remoteFile' && test `$(stat -c%s '$remoteFile') -eq $size"
    if ($existsSameSize) {
        $skipped += 1
        Write-Host "Skipping existing same-size file: $($pdf.Name)"
        continue
    }

    Write-Host "Uploading $($pdf.Name) ..."
    Invoke-UploadFile -LocalPath $pdf.FullName -RemoteTarget "${remote}:$remotePdfDir/"
    $uploaded += 1
}

Write-Host "Fixing PDF permissions ..."
Invoke-Remote "chown -R www-data:www-data '$remotePdfDir' && chmod -R a+rX '$remotePdfDir'"

Write-Host "Verifying remote Lenin PDFs ..."
Invoke-Remote "count=`$(find '$remotePdfDir' -maxdepth 1 -type f -name '*.pdf' | wc -l); test `"`$count`" -eq 60; bytes=`$(find '$remotePdfDir' -maxdepth 1 -type f -name '*.pdf' -printf '%s\n' | awk '{s+=`$1} END {print s+0}'); test `"`$bytes`" -gt 0; echo `"Remote Lenin PDFs verified: count=`$count bytes=`$bytes`""

Write-Host ""
Write-Host "Lenin PDF upload complete. Uploaded=$uploaded Skipped=$skipped"
Write-Host "Next safe step: run deploy/update_cloud.ps1 with -RebuildCorpus only after local smoke tests pass."
