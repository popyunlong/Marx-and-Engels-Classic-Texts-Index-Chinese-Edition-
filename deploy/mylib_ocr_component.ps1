[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('promote','rollback')][string]$Operation,
    [Parameter(Mandatory=$true)][string]$Expected,
    [string]$TargetRelease='',
    [string]$ServerHost='154.201.94.174',
    [string]$ServerUser='root',
    [int]$SshPort=22,
    [string]$IdentityFile='',
    [string]$MainServer='root@38.76.174.234',
    [Parameter(Mandatory=$true)][ValidateRange(1,2147483647)][int]$PreviousMainPid,
    [string]$ArtifactDirectory='D:\CodexData\builds\mylib-ocr',
    [hashtable]$Python=@{},
    [switch]$DryRun
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if ($ServerHost -ne '154.201.94.174' -or $ServerUser -ne 'root' -or $MainServer -ne 'root@38.76.174.234') { throw 'Unexpected OCR/main host; explicit code review required.' }
foreach ($value in @($Expected,$TargetRelease)) {
    if ($value -and $value -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw 'Unsafe OCR release identity.' }
}
$common=@('-p',"$SshPort",'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes')
if ($IdentityFile) { $common+=@('-i',(Resolve-Path -LiteralPath $IdentityFile).Path) }
$ssh=(Get-Command ssh -ErrorAction Stop).Source
$remote="${ServerUser}@${ServerHost}"
$gitSha=(& git rev-parse HEAD).Trim()
if ((& git symbolic-ref --quiet --short HEAD).Trim() -ne 'production' -or (& git status --porcelain=v1 --untracked-files=all)) { throw 'A clean production checkout is required.' }
if ($gitSha -ne (& git rev-parse origin/production).Trim()) { throw 'OCR source is not exactly origin/production.' }
& git merge-base --is-ancestor $gitSha origin/main
if ($LASTEXITCODE -ne 0) { throw 'OCR source is not checked main lineage.' }
$archive='';$archiveHash='';$remoteArchive=''
if ($Operation -eq 'promote') {
    $TargetRelease="$gitSha-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))-$(([Guid]::NewGuid().ToString('N')).Substring(0,8))"
    New-Item -ItemType Directory -Path $ArtifactDirectory -Force | Out-Null
    $archive=Join-Path ([IO.Path]::GetFullPath($ArtifactDirectory)) "$TargetRelease.tar.gz"
    if (-not $Python.Exe) { throw 'A verified production Python 3.10 executable is required.' }
    & $Python.Exe @($Python.Prefix) scripts/build_mylib_ocr_release.py --repo (Get-Location).Path --sha $gitSha --release-id $TargetRelease --parent $Expected --output $archive
    if ($LASTEXITCODE -ne 0) { throw 'OCR component archive failed.' }
    $archiveHash=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $remoteArchive="/var/tmp/mylib-ocr-$TargetRelease.tar.gz"
}
if ($DryRun) { Write-Output "OCR component prepared without a server transaction: $TargetRelease"; return }

# The main-host lock coordinates both servers. A second same-path node lock protects
# its own transaction. Neither lock is kept between subsequent book jobs.
$start=[Diagnostics.ProcessStartInfo]::new()
$start.FileName=$ssh
$start.UseShellExecute=$false
$start.CreateNoWindow=$true
$start.RedirectStandardInput=$true
$start.RedirectStandardOutput=$true
$start.RedirectStandardError=$true
foreach ($arg in @($common+@($MainServer,"flock -n /run/lock/marx-search-release.lock sh -c 'echo OCR_GLOBAL_LOCK; read release_input'"))) { $start.ArgumentList.Add($arg) }
$holder=[Diagnostics.Process]::new();$holder.StartInfo=$start
$acquired=$false
$started=$false
try {
    if (-not $holder.Start()) { throw 'Cannot create global lock connection.' }
    $started=$true
    $line=$holder.StandardOutput.ReadLineAsync()
    if (-not $line.Wait(15000) -or $line.Result -ne 'OCR_GLOBAL_LOCK') { throw 'Global production lock is unavailable.' }
    $acquired=$true
    if ($Operation -eq 'promote') {
        $runtimeText=& $ssh @common $MainServer "test ! -d /proc/$PreviousMainPid && curl -fsS http://127.0.0.1:8000/api/runtime"
        if ($LASTEXITCODE -ne 0) { throw 'The predecessor main process still exists, or main is not healthy.' }
        $runtime=($runtimeText -join "`n") | ConvertFrom-Json
        if ($runtime.mylib_ocr_protocol_version -ne 2) { throw 'Guarded main processing must be deployed before OCR.' }
        if ($runtime.app_release.git_sha -ne $gitSha) { throw 'OCR component and live main must use the same checked production revision.' }
        $scpArgs=@('-P',"$SshPort",'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes')
        if ($IdentityFile) {$scpArgs+=@('-i',(Resolve-Path -LiteralPath $IdentityFile).Path)}
        & scp @scpArgs $archive "${remote}:$remoteArchive"
        if ($LASTEXITCODE -ne 0) { throw 'OCR component upload failed; no activation attempted.' }
        $command="tar -xOf '$remoteArchive' deploy/mylib_ocr_transaction.py | /opt/mylib/venv/bin/python - promote --expected '$Expected' --target '$TargetRelease' --archive '$remoteArchive' --sha256 '$archiveHash'"
    } else {
        $command="/opt/mylib/venv/bin/python /opt/mylib-ocr/current/deploy/mylib_ocr_transaction.py rollback --expected '$Expected' --target '$TargetRelease'"
    }
    & $ssh @common $remote $command
    if ($LASTEXITCODE -ne 0) { throw 'OCR transaction failed; inspect preserved release event before any retry.' }
    Write-Output "OCR transaction completed with dispatch paused: $TargetRelease"
} finally {
    if ($started) {
        if ($acquired -and -not $holder.HasExited) {
            try {$holder.StandardInput.WriteLine('release');$holder.StandardInput.Flush()} catch {Write-Warning 'Global lock connection already closed.'}
        }
        if (-not $holder.HasExited -and -not $holder.WaitForExit(5000)) { $holder.Kill() }
    }
    $holder.Dispose()
}
