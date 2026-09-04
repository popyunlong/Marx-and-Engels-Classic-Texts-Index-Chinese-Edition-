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
$remoteRelease = "$RemoteDir.release.$stamp"
$didPromote = $false

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

    Write-Host "Staging an isolated candidate release; the live application tree remains untouched ..."
    Invoke-Remote "tar -xOf '$remoteArchive' ./deploy/stage_release.sh | bash -s -- '$RemoteDir' '$remoteRelease' '$remoteArchive'"

    Write-Host "Fixing permissions only inside the isolated candidate ..."
    Invoke-Remote "find '$remoteRelease' -xdev -type d -exec chmod a+rx {} + && find '$remoteRelease' -xdev -type f -exec chown www-data:www-data {} + -exec chmod a+r {} + && chmod 0755 '$remoteRelease/deploy/zero_downtime_restart.sh' '$remoteRelease/deploy/stage_release.sh'"
    Invoke-Remote-BestEffort "install -d -o www-data -g www-data -m 0700 /var/www/.marx_search_full /var/www/.marx_search_full/page_images"
    if ($FixCachePermissions) {
        Write-Host "Recursively fixing cache permissions because -FixCachePermissions was supplied ..."
        Invoke-Remote-BestEffort "chown -R www-data:www-data /var/www/.marx_search_full && chmod -R u+rwX /var/www/.marx_search_full"
    } else {
        Write-Host "Skipping recursive cache permission scan. Use -FixCachePermissions only when cache ownership is known to be wrong."
    }

    Write-Host "Installing candidate dependencies into an isolated target ..."
    Invoke-Remote "mkdir -p '$remoteRelease/.deploy-deps' && '$RemoteDir/.venv/bin/python' -m pip install --target '$remoteRelease/.deploy-deps' -r '$remoteRelease/requirements.txt'"

    Write-Host "Ensuring the licensed CJK font package for citation PDF reports ..."
    Invoke-Remote "if ! dpkg-query -W -f='`${Status}' fonts-noto-cjk 2>/dev/null | grep -q 'install ok installed'; then export DEBIAN_FRONTEND=noninteractive && apt-get update && apt-get install -y fonts-noto-cjk; fi"

    Write-Host "Compiling changed Python files ..."
    Invoke-Remote "cd '$remoteRelease' && PYTHONPATH='$remoteRelease/.deploy-deps' '$RemoteDir/.venv/bin/python' -m py_compile $($compileFiles -join ' ')"

    Write-Host "Backing up and migrating the membership database before loading the new app ..."
    # 必须位于 deployment_smoke 之前：后者会 import app，而 app 启动时会调用
    # init_membership_db。先显式做 WAL 一致性备份和有耗时上限的幂等迁移，
    # 任一步失败就在重启前终止，当前线上进程继续正常服务。
    Invoke-Remote "cd '$remoteRelease' && sudo -u www-data -H env PYTHONPATH='$remoteRelease/.deploy-deps' '$RemoteDir/.venv/bin/python' scripts/predeploy_membership_migration.py --max-seconds 30"

    Write-Host "Running server import smoke test before restart ..."
    # 远端合并 stderr 到 stdout，避免冒烟脚本日志经 ssh 的 stderr 在本地触发终止错误（exit code 仍会正确传回）。
    Invoke-Remote "cd '$remoteRelease' && PYTHONPATH='$remoteRelease/.deploy-deps' '$RemoteDir/.venv/bin/python' scripts/deployment_smoke.py --mode server 2>&1"

    if ($RebuildCorpus) {
        Write-Host "Rebuilding corpus.sqlite on server because -RebuildCorpus was supplied. This is a long-running foreground task."
        Invoke-Remote "cd '$RemoteDir' && . .venv/bin/activate && python build_index.py && python scripts/build_wenji_toc.py && python scripts/build_quanji_toc.py && python scripts/build_toc.py --book '列宁全集' && DBP=`$(python -c 'import build_index; print(build_index.DB_PATH)') && chown www-data:www-data `"`$DBP`" `"`$DBP.sha256`""
    } else {
        Write-Host "Skipping corpus rebuild. Use -RebuildCorpus after PDFs are uploaded and verified."
    }

    if ($SkipRestart) {
        Write-Host "Candidate validation completed; -SkipRestart leaves the live tree unchanged."
        Invoke-Remote-BestEffort "rm -rf -- '$remoteRelease' && rm -f -- '$remoteArchive'"
    } else {
        Write-Host "Performing zero-downtime Caddy/Waitress cutover ..."
        Invoke-Remote "MARX_APP_DIR='$RemoteDir' MARX_RELEASE_DIR='$remoteRelease' MARX_PATCH_ARCHIVE='$remoteArchive' bash '$remoteRelease/deploy/zero_downtime_restart.sh'"
        $didPromote = $true

        Write-Host "Applying final ownership to promoted files ..."
        Invoke-Remote-BestEffort "cd '$RemoteDir' && while IFS= read -r item; do case `"`$item`" in ''|'#'*) continue;; esac; if [ -e `"`$item`" ]; then chown www-data:www-data `"`$item`" 2>/dev/null || true; chmod a+r `"`$item`" 2>/dev/null || true; fi; done < deploy/cloud_patch_files.txt"

        Write-Host "Verifying runtime and core pages ..."
        # Large corpus startup can take a few seconds; retry for up to about 36 seconds.
        # Beyond /api/runtime (process up), also probe core pages / and /pricing
        # (features work): any 5xx makes curl -fsS fail. The cutover script has
        # already retained the candidate as a fallback throughout both drains.
        Invoke-Remote "for i in `$(seq 1 12); do curl -fsS http://127.0.0.1:8000/api/runtime >/dev/null 2>&1 && break; sleep 3; done; curl -fsS http://127.0.0.1:8000/api/runtime >/dev/null && curl -fsS http://127.0.0.1:8000/ >/dev/null && curl -fsS http://127.0.0.1:8000/pricing >/dev/null && systemctl is-active marx-search >/dev/null"

        Write-Host "Installing background workers and timers after the website cutover ..."
        # Worker imports app and loads corpus.sqlite into memory. It must restart on every
        # promoted code/corpus release even when its unit file itself is unchanged.
        Invoke-Remote "cd '$RemoteDir' && changed=0 && tmp_service=`$(mktemp) && sed -e 's|/opt/marx-search|$RemoteDir|g' deploy/marx-search-citation-worker.service > `"`$tmp_service`" && if ! cmp -s `"`$tmp_service`" /etc/systemd/system/marx-search-citation-worker.service 2>/dev/null; then cp `"`$tmp_service`" /etc/systemd/system/marx-search-citation-worker.service && changed=1; fi && rm -f `"`$tmp_service`" && if [ `"`$changed`" -eq 1 ]; then systemctl daemon-reload; fi && systemctl restart marx-search-citation-worker.service && systemctl is-active --quiet marx-search-citation-worker.service && if ! systemctl is-enabled --quiet marx-search-citation-worker.service; then systemctl enable marx-search-citation-worker.service >/dev/null; fi"
        Invoke-Remote "cd '$RemoteDir' && changed=0 && tmp_service=`$(mktemp) && sed -e 's|/opt/marx-search|$RemoteDir|g' deploy/marx-search-journal-alerts.service > `"`$tmp_service`" && if ! cmp -s `"`$tmp_service`" /etc/systemd/system/marx-search-journal-alerts.service 2>/dev/null; then cp `"`$tmp_service`" /etc/systemd/system/marx-search-journal-alerts.service && changed=1; fi && rm -f `"`$tmp_service`" && if ! cmp -s deploy/marx-search-journal-alerts.timer /etc/systemd/system/marx-search-journal-alerts.timer 2>/dev/null; then cp deploy/marx-search-journal-alerts.timer /etc/systemd/system/marx-search-journal-alerts.timer && changed=1; fi && if [ `"`$changed`" -eq 1 ]; then systemctl daemon-reload && systemctl restart marx-search-journal-alerts.timer; else if ! systemctl is-active --quiet marx-search-journal-alerts.timer; then systemctl start marx-search-journal-alerts.timer; fi; fi && if ! systemctl is-enabled --quiet marx-search-journal-alerts.timer; then systemctl enable marx-search-journal-alerts.timer >/dev/null; fi"
        Invoke-Remote "cd '$RemoteDir' && changed=0 && tmp_service=`$(mktemp) && sed -e 's|/opt/marx-search|$RemoteDir|g' deploy/marx-search-journal-process.service > `"`$tmp_service`" && if ! cmp -s `"`$tmp_service`" /etc/systemd/system/marx-search-journal-process.service 2>/dev/null; then cp `"`$tmp_service`" /etc/systemd/system/marx-search-journal-process.service && changed=1; fi && rm -f `"`$tmp_service`" && if ! cmp -s deploy/marx-search-journal-process.timer /etc/systemd/system/marx-search-journal-process.timer 2>/dev/null; then cp deploy/marx-search-journal-process.timer /etc/systemd/system/marx-search-journal-process.timer && changed=1; fi && if [ `"`$changed`" -eq 1 ]; then systemctl daemon-reload && systemctl restart marx-search-journal-process.timer; else if ! systemctl is-active --quiet marx-search-journal-process.timer; then systemctl start marx-search-journal-process.timer; fi; fi && if ! systemctl is-enabled --quiet marx-search-journal-process.timer; then systemctl enable marx-search-journal-process.timer >/dev/null; fi"
        # 邮件只允许管理员在控制台最终确认后发送；部署不得重新启用历史定时发送器。
        Invoke-Remote "systemctl disable --now marx-search-journal-send.timer >/dev/null 2>&1 || true"
        Invoke-Remote "cd '$RemoteDir' && sed -e 's|/opt/marx-search|$RemoteDir|g' deploy/marx-search-backup.service > /etc/systemd/system/marx-search-backup.service && cp deploy/marx-search-backup.timer /etc/systemd/system/marx-search-backup.timer && systemctl daemon-reload && systemctl enable --now marx-search-backup.timer"
        Invoke-Remote-BestEffort "systemctl start marx-search-backup.service; sleep 2; ls -1dt /var/backups/marx-search/*/ 2>/dev/null | head -1"
        Invoke-Remote-BestEffort "rm -rf -- '$remoteRelease' && rm -f -- '$remoteArchive'"
    }

    if ($SkipRestart) {
        Write-Host "Checking current running service status without rollback ..."
        Invoke-Remote-BestEffort "systemctl is-active marx-search >/dev/null && curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null && systemctl is-active marx-search-journal-alerts.timer >/dev/null && systemctl is-active marx-search-journal-process.timer >/dev/null"
    }

    if ($didPromote -and $deployedSha -ne "unknown") {
        $shaMarker = if ($treeDirty) { "$deployedSha-dirty" } else { "$deployedSha" }
        Write-Host "Recording deployed revision $shaMarker into $RemoteDir/DEPLOYED_SHA ..."
        Invoke-Remote-BestEffort "printf '%s\n' '$shaMarker' > '$RemoteDir/DEPLOYED_SHA'"
    }

    Write-Host ""
    if ($didPromote) {
        Write-Host "Cloud patch complete."
    } else {
        Write-Host "Validation complete; no live files or processes were replaced."
    }
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
