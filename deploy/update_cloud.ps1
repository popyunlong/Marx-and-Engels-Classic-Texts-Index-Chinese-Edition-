param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun,
    [switch]$SkipRestart,
    [switch]$KeepLocalArchive,
    [switch]$RebuildCorpus,
    [switch]$FixCachePermissions,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Read-DeployManifest {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        throw "Missing deploy manifest: $Path"
    }
    $items = @()
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $item = $line.Trim()
        if (-not $item -or $item.StartsWith("#")) {
            continue
        }
        $items += $item
    }
    if (-not $items.Count) {
        throw "Deploy manifest is empty: $Path"
    }
    return $items
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @(),

        [switch]$AllowFailure
    )

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @ArgumentList 2>&1 | ForEach-Object { Write-Host $_ }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "$Label failed with exit code $exitCode."
    }
    return $exitCode
}

function Invoke-Native-Capture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @()
    )

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @ArgumentList 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }

    if ($exitCode -ne 0) {
        $message = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        throw "$Label failed with exit code $exitCode.`n$message"
    }
    return (($output | Select-Object -Last 1) -as [string]).Trim()
}

function Invoke-Remote {
    param([string]$Command)
    Invoke-Native -Label "Remote command" -FilePath "ssh" -ArgumentList (@($sshOptions) + @("-p", "$Port", $remote, $Command)) | Out-Null
}

function Invoke-Remote-Capture {
    param([string]$Command)
    return Invoke-Native-Capture -Label "Remote command" -FilePath "ssh" -ArgumentList (@($sshOptions) + @("-p", "$Port", $remote, $Command))
}

function Invoke-Remote-BestEffort {
    param([string]$Command)
    $exitCode = Invoke-Native -Label "Remote best-effort command" -FilePath "ssh" -ArgumentList (@($sshOptions) + @("-p", "$Port", $remote, $Command)) -AllowFailure
    if ($exitCode -ne 0) {
        Write-Warning "Remote best-effort command failed, continuing: $Command"
    }
}

function Invoke-UploadArchive {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LocalArchive,

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
        $args += $scpOptions
        $args += @("-o", "BatchMode=yes", "-P", $Port, $LocalArchive, $RemoteTarget)

        Write-Host "  -> scp mode: $($attempt.Label)"
        $exitCode = Invoke-Native -Label "scp $($attempt.Label) upload" -FilePath "scp" -ArgumentList $args -AllowFailure
        if ($exitCode -eq 0) {
            return
        }

        Write-Warning "scp $($attempt.Label) mode failed."
    }

    throw "Failed to upload patch archive with both default and legacy scp modes."
}

Require-Command "ssh"
Require-Command "scp"
Require-Command "tar"
Require-Command "python"

$repoRoot = Split-Path -Parent $PSScriptRoot
$remote = "$User@${ServerHost}"
$remoteArchive = "/tmp/marx-cloud-patch.tar.gz"
$sshOptions = @("-n", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")
$scpOptions = @("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")
if ($IdentityFile -and (Test-Path $IdentityFile)) {
    $sshOptions += @("-i", $IdentityFile)
    $scpOptions += @("-i", $IdentityFile)
}

$files = Read-DeployManifest (Join-Path $PSScriptRoot "cloud_patch_files.txt")
$compileFiles = Read-DeployManifest (Join-Path $PSScriptRoot "cloud_compile_files.txt")

# Clean-tree guard + deployed-revision stamping.
# This script packages from the WORKING TREE (not from git). Without a guard the
# working tree can silently drift ahead of git: files get deployed but never
# committed, so "what is actually live" becomes unknowable. The guard makes the
# default rule "deploy == committed". After a successful deploy we stamp the
# deployed commit into <RemoteDir>/DEPLOYED_SHA so the live revision is auditable.
$deployedSha = "unknown"
$treeDirty = $false
if (Get-Command git -ErrorAction SilentlyContinue) {
    Push-Location $repoRoot
    try {
        & git rev-parse --is-inside-work-tree 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $deployedSha = (& git rev-parse HEAD 2>$null | Select-Object -First 1)
            $dirtyFiles = @()
            foreach ($line in (& git status --porcelain -- @files 2>$null)) {
                if ($line -and $line.Length -gt 3) { $dirtyFiles += $line.Substring(3).Trim('"') }
            }
            if ($dirtyFiles.Count -gt 0) {
                $treeDirty = $true
                Write-Host ""
                Write-Warning "Deploy manifest has UNCOMMITTED changes in the working tree:"
                foreach ($f in $dirtyFiles) { Write-Host "  * $f" }
                if ($AllowDirty) {
                    Write-Warning "Proceeding with a DIRTY deploy because -AllowDirty was supplied. DEPLOYED_SHA will be marked '-dirty'."
                } else {
                    throw "Refusing to deploy a dirty working tree. Commit these files first, or pass -AllowDirty to override."
                }
            }
        } else {
            Write-Warning "Clean-tree guard skipped: not a git work tree."
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Warning "Clean-tree guard skipped: git was not found on PATH."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stage = Join-Path $env:TEMP "marx-cloud-patch-$stamp"
$archive = Join-Path $env:TEMP "marx-cloud-patch-$stamp.tar.gz"

try {
    Write-Host "Running local deployment smoke test ..."
    Push-Location $repoRoot
    try {
        Invoke-Native -Label "Local deployment smoke test" -FilePath "python" -ArgumentList @("scripts\deployment_smoke.py", "--mode", "server") | Out-Null
    } finally {
        Pop-Location
    }

    Write-Host "Preparing cloud patch package ..."
    New-Item -ItemType Directory -Path $stage | Out-Null

    foreach ($file in $files) {
        $src = Join-Path $repoRoot $file
        if (-not (Test-Path $src)) {
            throw "Missing local path: $src"
        }
        $dst = Join-Path $stage $file
        New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }

    Push-Location $stage
    try {
        Invoke-Native -Label "Create patch archive" -FilePath "tar" -ArgumentList @("-czf", $archive, ".") | Out-Null
    } finally {
        Pop-Location
    }

    $archiveSizeMb = [Math]::Round((Get-Item $archive).Length / 1MB, 2)
    Write-Host "Patch package ready: $archiveSizeMb MB"

    if ($DryRun) {
        Write-Host "Dry run complete. No cloud connection was made."
        return
    }

    Write-Host "Checking remote project at $RemoteDir ..."
    Invoke-Remote "test -f '$RemoteDir/app.py' && test -d '$RemoteDir/templates' && test -d '$RemoteDir/data'"

    Write-Host "Removing incomplete lightweight backups if any ..."
    Invoke-Remote-BestEffort "find /opt -maxdepth 1 -type d \( -name 'marx-search.reader-backup.*' -o -name 'marx-search.quick-backup.*' \) -empty -exec rm -rf {} +"

    Write-Host "Uploading compact cloud patch to $remote ..."
    Invoke-UploadArchive -LocalArchive $archive -RemoteTarget "${remote}:$remoteArchive"

    Write-Host "Creating lightweight remote backup without PDFs or corpus data ..."
    $remoteBackup = Invoke-Remote-Capture "backup='$RemoteDir.cloud-backup.'`$(date +%Y%m%d-%H%M%S); mkdir -p `"`$backup`"; cd '$RemoteDir' && cp -a *.py DEPLOY_SERVER.md README.md requirements.txt `"`$backup/`" 2>/dev/null || true; for d in templates scripts config deploy; do if [ -e `"`$d`" ]; then mkdir -p `"`$backup/`$d`"; cp -a `"`$d/.`" `"`$backup/`$d/`" 2>/dev/null || true; fi; done; echo `"`$backup`""
    Write-Host "Remote backup: $remoteBackup"

    Write-Host "Pruning old cloud backups, keeping the 5 most recent ..."
    Invoke-Remote-BestEffort "ls -1dt '$RemoteDir'.cloud-backup.* 2>/dev/null | tail -n +6 | xargs -r rm -rf"

    Write-Host "Applying patch on server without touching PDFs or corpus data ..."
    Invoke-Remote "mkdir -p '$RemoteDir/templates' '$RemoteDir/scripts' '$RemoteDir/config' '$RemoteDir/deploy'"
    Invoke-Remote "tar -xzf '$remoteArchive' -C '$RemoteDir' && rm -f '$remoteArchive'"

    Write-Host "Fixing lightweight permissions ..."
    Invoke-Remote-BestEffort "cd '$RemoteDir' && chown www-data:www-data *.py templates/*.html scripts/*.py deploy/*.ps1 deploy/*.sh deploy/marx-search-journal-alerts.* deploy/marx-search-journal-send.* config/*.example config/books.yaml config/manifest.yaml config/volumes.yaml config/wenji_toc_overrides.yaml config/quanji_toc_overrides.yaml DEPLOY_SERVER.md 2>/dev/null || true && chmod -R a+rX templates scripts deploy config static 2>/dev/null || true && chmod a+r static/vendor/qrcode.min.js 2>/dev/null || true"
    Invoke-Remote-BestEffort "install -d -o www-data -g www-data -m 0700 /var/www/.marx_search_full /var/www/.marx_search_full/page_images"
    if ($FixCachePermissions) {
        Write-Host "Recursively fixing cache permissions because -FixCachePermissions was supplied ..."
        Invoke-Remote-BestEffort "chown -R www-data:www-data /var/www/.marx_search_full && chmod -R u+rwX /var/www/.marx_search_full"
    } else {
        Write-Host "Skipping recursive cache permission scan. Use -FixCachePermissions only when cache ownership is known to be wrong."
    }

    Write-Host "Compiling changed Python files ..."
    Invoke-Remote "cd '$RemoteDir' && . .venv/bin/activate && python -m py_compile $($compileFiles -join ' ')"

    Write-Host "Running server import smoke test before restart ..."
    # 远端合并 stderr 到 stdout，避免冒烟脚本日志经 ssh 的 stderr 在本地触发终止错误（exit code 仍会正确传回）。
    Invoke-Remote "cd '$RemoteDir' && . .venv/bin/activate && python scripts/deployment_smoke.py --mode server 2>&1"

    if ($RebuildCorpus) {
        Write-Host "Rebuilding corpus.sqlite on server because -RebuildCorpus was supplied. This is a long-running foreground task."
        Invoke-Remote "cd '$RemoteDir' && . .venv/bin/activate && python build_index.py && python scripts/build_wenji_toc.py && python scripts/build_quanji_toc.py && python scripts/build_toc.py --book '列宁全集' && DBP=`$(python -c 'import build_index; print(build_index.DB_PATH)') && chown www-data:www-data `"`$DBP`" `"`$DBP.sha256`""
    } else {
        Write-Host "Skipping corpus rebuild. Use -RebuildCorpus after PDFs are uploaded and verified."
    }

    Write-Host "Installing journal alert collect timer if systemd files changed ..."
    Invoke-Remote "cd '$RemoteDir' && changed=0 && tmp_service=`$(mktemp) && sed -e 's|/opt/marx-search|$RemoteDir|g' deploy/marx-search-journal-alerts.service > `"`$tmp_service`" && if ! cmp -s `"`$tmp_service`" /etc/systemd/system/marx-search-journal-alerts.service 2>/dev/null; then cp `"`$tmp_service`" /etc/systemd/system/marx-search-journal-alerts.service && changed=1; fi && rm -f `"`$tmp_service`" && if ! cmp -s deploy/marx-search-journal-alerts.timer /etc/systemd/system/marx-search-journal-alerts.timer 2>/dev/null; then cp deploy/marx-search-journal-alerts.timer /etc/systemd/system/marx-search-journal-alerts.timer && changed=1; fi && if [ `"`$changed`" -eq 1 ]; then systemctl daemon-reload && systemctl restart marx-search-journal-alerts.timer; else if ! systemctl is-active --quiet marx-search-journal-alerts.timer; then systemctl start marx-search-journal-alerts.timer; fi; fi && if ! systemctl is-enabled --quiet marx-search-journal-alerts.timer; then systemctl enable marx-search-journal-alerts.timer >/dev/null; fi"

    Write-Host "Installing journal alert send timer if systemd files changed ..."
    Invoke-Remote "cd '$RemoteDir' && changed=0 && tmp_service=`$(mktemp) && sed -e 's|/opt/marx-search|$RemoteDir|g' deploy/marx-search-journal-send.service > `"`$tmp_service`" && if ! cmp -s `"`$tmp_service`" /etc/systemd/system/marx-search-journal-send.service 2>/dev/null; then cp `"`$tmp_service`" /etc/systemd/system/marx-search-journal-send.service && changed=1; fi && rm -f `"`$tmp_service`" && if ! cmp -s deploy/marx-search-journal-send.timer /etc/systemd/system/marx-search-journal-send.timer 2>/dev/null; then cp deploy/marx-search-journal-send.timer /etc/systemd/system/marx-search-journal-send.timer && changed=1; fi && if [ `"`$changed`" -eq 1 ]; then systemctl daemon-reload && systemctl restart marx-search-journal-send.timer; else if ! systemctl is-active --quiet marx-search-journal-send.timer; then systemctl start marx-search-journal-send.timer; fi; fi && if ! systemctl is-enabled --quiet marx-search-journal-send.timer; then systemctl enable marx-search-journal-send.timer >/dev/null; fi"

    Write-Host "Installing daily data backup timer ..."
    Invoke-Remote "cd '$RemoteDir' && sed -e 's|/opt/marx-search|$RemoteDir|g' deploy/marx-search-backup.service > /etc/systemd/system/marx-search-backup.service && cp deploy/marx-search-backup.timer /etc/systemd/system/marx-search-backup.timer && systemctl daemon-reload && systemctl enable --now marx-search-backup.timer"

    Write-Host "Running one backup now to verify it works ..."
    Invoke-Remote-BestEffort "systemctl start marx-search-backup.service; sleep 2; ls -1dt /var/backups/marx-search/*/ 2>/dev/null | head -1"

    if ($SkipRestart) {
        Write-Host "Skipping service restart because -SkipRestart was supplied."
    } else {
        Write-Host "Restarting service ..."
        Invoke-Remote "systemctl restart marx-search"

        Write-Host "Verifying runtime and core pages ..."
        # Large corpus startup can take a few seconds; retry for up to about 36 seconds.
        # Beyond /api/runtime (process up), also probe core pages / and /pricing
        # (features work): any 5xx makes curl -fsS fail and triggers the rollback below.
        try {
            Invoke-Remote "for i in `$(seq 1 12); do curl -fsS http://127.0.0.1:8000/api/runtime >/dev/null 2>&1 && break; sleep 3; done; curl -fsS http://127.0.0.1:8000/api/runtime >/dev/null && curl -fsS http://127.0.0.1:8000/ >/dev/null && curl -fsS http://127.0.0.1:8000/pricing >/dev/null && systemctl is-active marx-search >/dev/null && systemctl is-active marx-search-journal-alerts.timer >/dev/null"
        } catch {
            Write-Warning "Runtime verification failed. Attempting rollback from $remoteBackup ..."
            Invoke-Remote "test -n '$remoteBackup' && test -d '$remoteBackup' && cd '$RemoteDir' && cp -a '$remoteBackup/.' '$RemoteDir/' && systemctl restart marx-search"
            Invoke-Remote "for i in `$(seq 1 12); do curl -fsS http://127.0.0.1:8000/api/runtime >/dev/null 2>&1 && break; sleep 3; done; curl -fsS http://127.0.0.1:8000/api/runtime >/dev/null && curl -fsS http://127.0.0.1:8000/ >/dev/null && systemctl is-active marx-search >/dev/null"
            throw "Deployment verification failed and rollback was applied from $remoteBackup."
        }
    }

    if ($SkipRestart) {
        Write-Host "Checking current running service status without rollback ..."
        Invoke-Remote-BestEffort "systemctl is-active marx-search >/dev/null && curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null && systemctl is-active marx-search-journal-alerts.timer >/dev/null"
    }

    if ($deployedSha -ne "unknown") {
        $shaMarker = if ($treeDirty) { "$deployedSha-dirty" } else { "$deployedSha" }
        Write-Host "Recording deployed revision $shaMarker into $RemoteDir/DEPLOYED_SHA ..."
        Invoke-Remote-BestEffort "printf '%s\n' '$shaMarker' > '$RemoteDir/DEPLOYED_SHA'"
    }

    Write-Host ""
    Write-Host "Cloud patch complete."
    Write-Host "Open: https://mazhuzuojiansuo.com/library"
} finally {
    if (-not $KeepLocalArchive -and (Test-Path $archive)) {
        Remove-Item -LiteralPath $archive -Force
    } elseif ($KeepLocalArchive -and (Test-Path $archive)) {
        Write-Host "Kept local archive: $archive"
    }
    if (Test-Path $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
