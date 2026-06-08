param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [string]$LocalDb = "",
    [switch]$DryRun
)

# 安全热替换云端 corpus.sqlite：
#   1) 校验本地 DB 与其 .sha256 一致；
#   2) scp 上传 DB+.sha256 到 /tmp，在服务器端再次校验 sha256；
#   3) 备份现网 DB（带时间戳）以便回滚；
#   4) 原子 mv 入 data/，chown www-data；
#   5) systemctl restart marx-search + 健康轮询；不健康则自动回滚旧 DB 并重启。
# 服务器启动时把 DB 读入内存后即关闭文件句柄，故磁盘替换安全、仅重启时生效。

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $LocalDb) { $LocalDb = Join-Path $repoRoot "data\corpus.sqlite" }
$LocalSha = "$LocalDb.sha256"

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Missing required command: $Name" }
}
Require-Command "ssh"; Require-Command "scp"

if (-not (Test-Path $LocalDb)) { throw "Local DB not found: $LocalDb" }
if (-not (Test-Path $LocalSha)) { throw "Local sha256 sidecar not found: $LocalSha (run scripts/write_release_metadata.py)" }

# 本地一致性校验
$calc = (Get-FileHash -Algorithm SHA256 -LiteralPath $LocalDb).Hash.ToLower()
$expected = ((Get-Content -LiteralPath $LocalSha -Raw).Trim().Split()[0]).ToLower()
if ($calc -ne $expected) { throw "Local DB sha256 mismatch: file=$calc sidecar=$expected" }
$sizeMb = [math]::Round((Get-Item $LocalDb).Length / 1MB, 1)
Write-Host "Local DB OK: $LocalDb ($sizeMb MB) sha256=$calc"

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
$tmpDb = "/tmp/corpus.sqlite.upload"
$tmpSha = "/tmp/corpus.sqlite.upload.sha256"

# 0) connectivity
Invoke-Remote "test -f '$remoteDb' && echo remote-db-present"

# 1) upload to /tmp
Invoke-Scp $LocalDb $tmpDb
Invoke-Scp $LocalSha $tmpSha

# 2) verify sha256 on server
Invoke-Remote "cd /tmp && calc=`$(sha256sum corpus.sqlite.upload | awk '{print `$1}') && want=`$(awk '{print `$1}' corpus.sqlite.upload.sha256) && test `"`$calc`" = `"`$want`" && echo SHA_OK || { echo SHA_MISMATCH `$calc `$want; exit 1; }"

# 3) backup current DB, 4) atomic swap, chown
Invoke-Remote "cp -a '$remoteDb' '$remoteBackup' && (cp -a '$remoteSha' '$remoteBackup.sha256' 2>/dev/null || true) && echo backed-up:'$remoteBackup'"
Invoke-Remote "owner=`$(stat -c '%U:%G' '$remoteDb') && mv '$tmpDb' '$remoteDb' && mv '$tmpSha' '$remoteSha' && chown `$owner '$remoteDb' '$remoteSha' && echo swapped"

# 5) restart + health, rollback on failure
if ($DryRun) { Write-Host "DryRun: skip restart"; return }
Write-Host "Restarting marx-search and verifying health ..."
$health = "for i in `$(seq 1 15); do curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null 2>&1 && break; sleep 3; done; curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null && systemctl is-active --quiet marx-search && echo HEALTH_OK"
try {
    Invoke-Remote "systemctl restart marx-search && $health"
    Write-Host "Deployment OK: corpus.sqlite swapped and service healthy. Backup: $remoteBackup"
} catch {
    Write-Warning "Health check failed — rolling back DB from $remoteBackup ..."
    Invoke-Remote "cp -a '$remoteBackup' '$remoteDb' && (cp -a '$remoteBackup.sha256' '$remoteSha' 2>/dev/null || true) && systemctl restart marx-search && $health"
    throw "DB deployment failed and was rolled back from $remoteBackup."
}
