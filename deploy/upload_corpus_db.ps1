param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [string]$LocalDb = "",
    [switch]$ReuseRemoteUpload,
    [switch]$DryRun
)

# 安全热替换云端 corpus.sqlite：
#   1) 校验本地 DB 与其 .sha256 一致；
#   2) scp 上传 DB+.sha256 到 /tmp，在服务器端再次校验 sha256；
#   3) 备份现网 DB，并把候选复制到 data/ 下的全新 incoming 路径；
#   4) 旧库改名为 retired 后，incoming 才原子改名为正式库（绝不覆盖/删除现有 DB）；
#   5) 主站与引文工作进程作为同一个语料版本单元重启并健康轮询；
#      不健康时新库改名留档、旧库原名恢复。
# 服务器启动时把 DB 读入内存后即关闭文件句柄，故磁盘替换安全、仅重启时生效。

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $LocalDb) { $LocalDb = Join-Path $repoRoot "data\corpus.sqlite" }
$LocalSha = "$LocalDb.sha256"
$GateScript = Join-Path $repoRoot "scripts\corpus_regression_gate.py"
$BooksConfig = Join-Path $repoRoot "config\books.yaml"
$ManifestConfig = Join-Path $repoRoot "config\manifest.yaml"

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Missing required command: $Name" }
}
Require-Command "ssh"; Require-Command "scp"; Require-Command "python"

if (-not (Test-Path $LocalDb)) { throw "Local DB not found: $LocalDb" }
if (-not (Test-Path $LocalSha)) { throw "Local sha256 sidecar not found: $LocalSha (run scripts/write_release_metadata.py)" }
if (-not (Test-Path $GateScript)) { throw "Corpus regression gate not found: $GateScript" }

# 本地一致性校验
$calc = (Get-FileHash -Algorithm SHA256 -LiteralPath $LocalDb).Hash.ToLower()
$expected = ((Get-Content -LiteralPath $LocalSha -Raw).Trim().Split()[0]).ToLower()
if ($calc -ne $expected) { throw "Local DB sha256 mismatch: file=$calc sidecar=$expected" }
$sizeMb = [math]::Round((Get-Item $LocalDb).Length / 1MB, 1)
Write-Host "Local DB OK: $LocalDb ($sizeMb MB) sha256=$calc"

# Fail before any upload/swap if the candidate does not cover the configured
# public catalogue. The server-side invocation below additionally compares it
# with the live database, so existing content cannot silently disappear.
& python $GateScript --candidate $LocalDb --books $BooksConfig --manifest $ManifestConfig
if ($LASTEXITCODE -ne 0) { throw "Local corpus regression gate rejected the candidate DB." }

$sshTarget = "$User@$ServerHost"
$sshBase = @("-i", $IdentityFile, "-p", $Port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")

function Invoke-Remote([string]$Cmd) {
    $a = @(); $a += $sshBase; $a += @($sshTarget, $Cmd)
    Write-Host "ssh> $Cmd"
    if ($DryRun) { return }
    & ssh @a
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed (exit $LASTEXITCODE): $Cmd" }
}
function Invoke-Scp([string]$Local, [string]$RemoteTarget) {
    foreach ($extra in @(@(), @("-O"))) {
        $a = @("-i", $IdentityFile, "-P", $Port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
        $a += $extra; $a += @($Local, "$sshTarget`:$RemoteTarget")
        Write-Host "scp> $Local -> $RemoteTarget $($extra -join ' ')"
        if ($DryRun) { return }
        & scp @a
        if ($LASTEXITCODE -eq 0) { return }
        Write-Warning "scp failed (mode '$($extra -join ' ')'), retrying ..."
    }
    throw "Failed to scp $Local"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$remoteDb = "$RemoteDir/data/corpus.sqlite"
$remoteSha = "$remoteDb.sha256"
$remoteBackup = "$remoteDb.bak-$stamp"
$remoteIncoming = "$remoteDb.incoming-$stamp"
$remoteRetired = "$remoteDb.retired-$stamp"
$remoteFailed = "$remoteDb.failed-$stamp"
$tmpDb = "/tmp/corpus.sqlite.upload"
$tmpSha = "/tmp/corpus.sqlite.upload.sha256"
$tmpGate = "/tmp/corpus_regression_gate.py"

# 0) connectivity
Invoke-Remote "test -f '$remoteDb' && test -f '$remoteSha' && test ! -e '$remoteBackup' && test ! -e '$remoteIncoming' && test ! -e '$remoteRetired' && test ! -e '$remoteFailed' && echo remote-db-present"

# 1) upload to /tmp, or explicitly reuse a prior complete upload.  Reuse is
# still guarded by the same server-side checksum comparison below.
if ($ReuseRemoteUpload) {
    Invoke-Remote "test -f '$tmpDb' && test -f '$tmpSha' && echo reusing-remote-upload"
} else {
    Invoke-Scp $LocalDb $tmpDb
    Invoke-Scp $LocalSha $tmpSha
}

# 2) verify sha256 on server
Invoke-Remote "cd /tmp && calc=`$(sha256sum corpus.sqlite.upload | awk '{print `$1}') && want=`$(awk '{print `$1}' corpus.sqlite.upload.sha256 | tr -d '\r') && test `"`$calc`" = `"`$want`" && printf '%s\n' `"`$want`" > corpus.sqlite.upload.sha256 && echo SHA_OK || { echo SHA_MISMATCH `$calc `$want; exit 1; }"

# 2b) hard no-regression gate. Both databases are opened read-only; no backup,
# move, restart, or other live mutation happens until this command succeeds.
Invoke-Scp $GateScript $tmpGate
Invoke-Remote "'$RemoteDir/.venv/bin/python' '$tmpGate' --candidate '$tmpDb' --baseline '$remoteDb' --books '$RemoteDir/config/books.yaml' --manifest '$RemoteDir/config/manifest.yaml'"

# 3) Keep a byte-for-byte backup, then stage a second verified copy beside the
# live DB.  The uploaded /tmp file is also retained for forensic recovery.
Invoke-Remote "cp -a '$remoteDb' '$remoteBackup' && (cp -a '$remoteSha' '$remoteBackup.sha256' 2>/dev/null || true) && echo backed-up:'$remoteBackup'"
Invoke-Remote "owner=`$(stat -c '%U:%G' '$remoteDb') && cp --reflink=auto --preserve=mode,timestamps '$tmpDb' '$remoteIncoming' && cp --preserve=mode,timestamps '$tmpSha' '$remoteIncoming.sha256' && calc=`$(sha256sum '$remoteIncoming' | awk '{print `$1}') && want=`$(awk '{print `$1}' '$remoteIncoming.sha256' | tr -d '\r') && test `"`$calc`" = `"`$want`" && chown `$owner '$remoteIncoming' '$remoteIncoming.sha256' && echo incoming-ready"

# 4) No-overwrite atomic rename.  Every destination was proven absent above;
# the old live DB remains intact under a timestamped retired name.
Invoke-Remote "owner=`$(stat -c '%U:%G' '$remoteDb') && mv '$remoteDb' '$remoteRetired' && mv '$remoteSha' '$remoteRetired.sha256' && mv '$remoteIncoming' '$remoteDb' && mv '$remoteIncoming.sha256' '$remoteSha' && chown `$owner '$remoteDb' '$remoteSha' && echo swapped-old-retained:'$remoteRetired'"

# 5) restart + health, rollback on failure
if ($DryRun) { Write-Host "DryRun: skip restart"; return }
Write-Host "Restarting marx-search + citation worker and verifying one corpus generation ..."
$health = "for i in `$(seq 1 15); do curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null 2>&1 && break; sleep 3; done; curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null && systemctl is-active --quiet marx-search && systemctl is-active --quiet marx-search-citation-worker && echo HEALTH_OK"
try {
    Invoke-Remote "systemctl restart marx-search marx-search-citation-worker && $health"
    Write-Host "Deployment OK: corpus.sqlite swapped and both corpus consumers are healthy. Old DB retained: $remoteRetired; backup: $remoteBackup"
} catch {
    Write-Warning "Health check failed — preserving the failed DB and restoring the retired DB ..."
    Invoke-Remote "systemctl stop marx-search marx-search-citation-worker 2>/dev/null || true; if [ -f '$remoteDb' ]; then mv '$remoteDb' '$remoteFailed'; fi; if [ -f '$remoteSha' ]; then mv '$remoteSha' '$remoteFailed.sha256'; fi; if [ -f '$remoteRetired' ]; then mv '$remoteRetired' '$remoteDb'; if [ -f '$remoteRetired.sha256' ]; then mv '$remoteRetired.sha256' '$remoteSha'; fi; else cp -a '$remoteBackup' '$remoteDb'; cp -a '$remoteBackup.sha256' '$remoteSha' 2>/dev/null || true; fi; systemctl restart marx-search marx-search-citation-worker && $health"
    throw "DB deployment failed. Old DB restored; failed candidate retained at $remoteFailed."
}
