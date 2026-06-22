#requires -Version 5.1
<#
  sync_members_local.ps1
  Pull membership / account data FROM the production server TO this PC, so that if the
  site is ever taken down the member data is already on the operator's machine and can be
  migrated immediately. This script NEVER modifies the server; it only reads.

  Modes:
    Incremental (default): download the append-only payment ledger
        (<RemoteAppData>/member_exports/members.ndjson) and merge any NEW lines into a
        local cumulative copy. Cheap and idempotent -- meant to run every minute, so a
        member's record lands locally within ~1 minute of payment.
    Full: take a consistent (WAL-safe) snapshot of membership.sqlite3, download it, and
        also export a readable JSON of users/subscriptions/orders/plans. Meant to run once
        a day so the entire roster (including all account fields) is captured.

  Auth reuses the deploy SSH key (id_marx_cloud_ed25519). Safe to run repeatedly.

  Examples:
    powershell -ExecutionPolicy Bypass -File deploy\sync_members_local.ps1 -Mode Full
    powershell -ExecutionPolicy Bypass -File deploy\sync_members_local.ps1 -Mode Incremental
#>
param(
    [ValidateSet("Full", "Incremental")]
    [string]$Mode = "Incremental",
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [string]$RemoteAppData = "/var/www/.marx_search_full",
    [string]$RemotePython = "/opt/marx-search/.venv/bin/python",
    [string]$LocalDir = "$env:USERPROFILE\marx-member-backups",
    [int]$KeepDays = 60
)

$ErrorActionPreference = "Stop"

$incDir = Join-Path $LocalDir "incremental"
$fullDir = Join-Path $LocalDir "full"
$logFile = Join-Path $LocalDir "sync.log"
New-Item -ItemType Directory -Force -Path $LocalDir, $incDir, $fullDir | Out-Null

function Write-Log {
    param([string]$Message, [switch]$Persist)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    if ($Persist) {
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    }
}

$remote = "$User@$ServerHost"
$sshOpts = @("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20")
$scpOpts = @("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20")
if ($IdentityFile -and (Test-Path $IdentityFile)) {
    $sshOpts += @("-i", $IdentityFile)
    $scpOpts += @("-i", $IdentityFile)
}

function Invoke-Ssh {
    param([string]$Command, [switch]$AllowFailure)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & ssh @sshOpts -n -p $Port $remote $Command 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw ("ssh failed (exit {0}): {1}`n{2}" -f $code, $Command, ($out -join "`n"))
    }
    return [pscustomobject]@{ Code = $code; Output = (($out | ForEach-Object { "$_" }) -join "`n").Trim() }
}

function Copy-FromRemote {
    param([string]$RemotePath, [string]$LocalPath)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & scp @scpOpts -P $Port "${remote}:$RemotePath" $LocalPath 2>&1 | ForEach-Object { Write-Host $_ }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw ("scp failed (exit {0}) for {1}" -f $code, $RemotePath) }
}

$remoteNdjson = "$RemoteAppData/member_exports/members.ndjson"
$remoteDb = "$RemoteAppData/membership.sqlite3"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Sync-Incremental {
    $cumulative = Join-Path $incDir "members.ndjson"
    $present = (Invoke-Ssh "test -f '$remoteNdjson' && echo yes || echo no").Output
    if ($present -ne "yes") {
        Write-Log "No payment ledger on server yet (no member payments recorded since the feature went live)."
        return
    }
    $tmp = Join-Path $incDir ".members.remote.tmp"
    if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Force }
    Copy-FromRemote $remoteNdjson $tmp

    $remoteLines = [System.IO.File]::ReadAllLines($tmp)
    $localLines = @()
    if (Test-Path $cumulative) { $localLines = [System.IO.File]::ReadAllLines($cumulative) }

    if ($remoteLines.Count -gt $localLines.Count) {
        $delta = $remoteLines.Count - $localLines.Count
        $new = $remoteLines[$localLines.Count..($remoteLines.Count - 1)]
        [System.IO.File]::AppendAllText($cumulative, (($new -join "`n") + "`n"), $utf8NoBom)
        Write-Log ("Appended {0} new payment record(s); local total {1}." -f $delta, $remoteLines.Count) -Persist
    }
    elseif ($remoteLines.Count -eq $localLines.Count) {
        Write-Log ("Up to date ({0} record(s))." -f $remoteLines.Count)
    }
    else {
        # Server ledger shorter than local copy -> unexpected reset/rotation. Never lose local
        # history: archive what we have, then mirror the (shorter) server file as the new base.
        $archive = Join-Path $incDir ("members.archived-{0}.ndjson" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
        Move-Item -LiteralPath $cumulative -Destination $archive -Force
        Copy-Item -LiteralPath $tmp -Destination $cumulative -Force
        Write-Log ("WARNING: server ledger ({0}) shorter than local ({1}); archived local to {2} and mirrored server." -f $remoteLines.Count, $localLines.Count, $archive) -Persist
    }
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

function Sync-Full {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $remoteTmpDb = "/tmp/membership-backup-$stamp.sqlite3"
    $remoteTmpJson = "/tmp/members-$stamp.json"

    # WAL-safe consistent snapshot using the app's venv python (always present on the server).
    $pyBackup = "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()"
    Invoke-Ssh "$RemotePython -c '$pyBackup' '$remoteDb' '$remoteTmpDb'" | Out-Null

    # Readable JSON export (users/subscriptions/orders/plans) generated server-side from the snapshot.
    $pyJson = 'import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); c.row_factory=sqlite3.Row; o={t:[dict(r) for r in c.execute("SELECT * FROM "+t)] for t in ("users","subscriptions","orders","plans")}; c.close(); open(sys.argv[2],"w",encoding="utf-8").write(json.dumps(o,ensure_ascii=False,indent=2,default=str))'
    Invoke-Ssh "$RemotePython -c '$pyJson' '$remoteTmpDb' '$remoteTmpJson'" -AllowFailure | Out-Null

    $localDb = Join-Path $fullDir "membership-$stamp.sqlite3"
    $localJson = Join-Path $fullDir "membership-$stamp.json"
    Copy-FromRemote $remoteTmpDb $localDb
    try { Copy-FromRemote $remoteTmpJson $localJson } catch { Write-Log "JSON export not downloaded ($($_.Exception.Message)); the .sqlite3 is the authoritative full copy." -Persist }
    Invoke-Ssh "rm -f '$remoteTmpDb' '$remoteTmpJson'" -AllowFailure | Out-Null

    $sizeKb = [math]::Round((Get-Item -LiteralPath $localDb).Length / 1KB, 1)
    Write-Log ("Full snapshot downloaded -> {0} ({1} KB)." -f $localDb, $sizeKb) -Persist

    # Keep the incremental ledger fresh as part of the daily run too.
    Sync-Incremental

    # Retention: prune full snapshots older than KeepDays.
    $cutoff = (Get-Date).AddDays(-1 * [math]::Abs($KeepDays))
    $pruned = 0
    Get-ChildItem -LiteralPath $fullDir -File | Where-Object { $_.LastWriteTime -lt $cutoff } | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
        $pruned++
    }
    if ($pruned -gt 0) { Write-Log ("Pruned {0} snapshot(s) older than {1} day(s)." -f $pruned, $KeepDays) -Persist }
}

Write-Log ("sync_members_local starting (Mode={0}, server={1}, local={2})." -f $Mode, $remote, $LocalDir)
try {
    if ($Mode -eq "Full") { Sync-Full } else { Sync-Incremental }
    Write-Log "Done."
}
catch {
    Write-Log ("ERROR: {0}" -f $_.Exception.Message) -Persist
    throw
}
