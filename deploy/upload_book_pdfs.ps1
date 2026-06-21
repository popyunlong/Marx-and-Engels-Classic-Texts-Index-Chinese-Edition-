param(
    [Parameter(Mandatory = $true)]
    [string]$Folder,                       # pdfs 下的子目录名，如 "马克思恩格斯全集（第二版）"
    [int]$ExpectedCount = 0,               # >0 时校验本地/远端文件数，防误传
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun
)

# 定向上传「某一本书的整目录 PDF」到服务器（增量、可续传）：逐文件 scp，远端已存在且同字节数则跳过，
# 上传后修权限并核对文件数/字节数。仿 deploy/upload_lenin_pdfs.ps1，参数化以支持任意书库目录。
# 不触碰 corpus.sqlite、不重启服务——仅把 PDF 落到服务器，供阅读器渲染页面图像 + 后续注入引用。

$ErrorActionPreference = "Stop"

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Missing required command: $Name" }
}
Require-Command "ssh"; Require-Command "scp"

$repoRoot = Split-Path -Parent $PSScriptRoot
$localDir = Join-Path (Join-Path $repoRoot "pdfs") $Folder
if (-not (Test-Path -LiteralPath $localDir)) { throw "Missing local PDF directory: $localDir" }

$pdfs = @(Get-ChildItem -LiteralPath $localDir -Filter "*.pdf" -File | Sort-Object Name)
if ($pdfs.Count -eq 0) { throw "No PDFs found under $localDir" }
if ($ExpectedCount -gt 0 -and $pdfs.Count -ne $ExpectedCount) {
    throw "Expected $ExpectedCount PDFs under '$Folder', found $($pdfs.Count). Refusing to upload."
}

$remote = "$User@${ServerHost}"
$remotePdfDir = "$RemoteDir/pdfs/$Folder"
$sshOptions = @("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")
if ($IdentityFile -and (Test-Path $IdentityFile)) { $sshOptions += @("-i", $IdentityFile) }

function Invoke-Remote([string]$Command) {
    & ssh @sshOptions -p $Port $remote $Command
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed: $Command" }
}
function Invoke-Remote-Test([string]$Command) {
    & ssh @sshOptions -p $Port $remote $Command | Out-Null
    return ($LASTEXITCODE -eq 0)
}
function Invoke-UploadFile([string]$LocalPath, [string]$RemoteTarget) {
    foreach ($extra in @(@(), @("-O"))) {
        $args = @(); $args += $extra; $args += $sshOptions
        $args += @("-o", "BatchMode=yes", "-P", $Port, $LocalPath, $RemoteTarget)
        & scp @args
        if ($LASTEXITCODE -eq 0) { return }
        Write-Warning "scp failed for $LocalPath (mode '$($extra -join ' ')'), retrying ..."
    }
    throw "Failed to upload $LocalPath with both default and legacy scp modes."
}

$totalGb = [Math]::Round((($pdfs | Measure-Object -Property Length -Sum).Sum) / 1GB, 2)
Write-Host "Book PDF upload plan: folder='$Folder' files=$($pdfs.Count) size=$totalGb GB"
Write-Host "Remote target: ${remote}:$remotePdfDir"
if ($DryRun) { Write-Host "Dry run: no cloud connection or upload will be made."; return }

Write-Host "Preparing remote directory ..."
Invoke-Remote "test -d '$RemoteDir/pdfs' && mkdir -p '$remotePdfDir'"

$uploaded = 0; $skipped = 0
foreach ($pdf in $pdfs) {
    $size = [int64]$pdf.Length
    $remoteFile = "$remotePdfDir/$($pdf.Name)"
    if (Invoke-Remote-Test "test -f '$remoteFile' && test `$(stat -c%s '$remoteFile') -eq $size") {
        $skipped += 1; Write-Host "Skip (same size): $($pdf.Name)"; continue
    }
    Write-Host "Uploading $($pdf.Name) ($([Math]::Round($size/1MB,1)) MB) ..."
    Invoke-UploadFile $pdf.FullName "${remote}:$remotePdfDir/"
    $uploaded += 1
}

Write-Host "Fixing permissions ..."
Invoke-Remote "chown -R www-data:www-data '$remotePdfDir' && chmod -R a+rX '$remotePdfDir'"

Write-Host "Verifying remote PDFs ..."
Invoke-Remote "count=`$(find '$remotePdfDir' -maxdepth 1 -type f -name '*.pdf' | wc -l); echo `"remote pdf count=`$count`"; test `$count -ge $($pdfs.Count)"

Write-Host ""
Write-Host "PDF upload complete. Uploaded=$uploaded Skipped=$skipped"
