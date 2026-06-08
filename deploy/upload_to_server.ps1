param(
    [Parameter(Mandatory = $true)]
    [Alias("Host")]
    [string]$ServerHost,

    [Parameter(Mandatory = $true)]
    [string]$User,

    [string]$RemoteDir = "/opt/marx-search",

    [int]$Port = 22
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

Require-Command "ssh"
Require-Command "scp"

function Invoke-Upload {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LocalPath,

        [Parameter(Mandatory = $true)]
        [string]$RemoteTarget,

        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $attempts = @(
        @{ Label = "default"; ExtraArgs = @() },
        @{ Label = "legacy"; ExtraArgs = @("-O") }
    )

    foreach ($attempt in $attempts) {
        $args = @()
        $args += $attempt.ExtraArgs
        $args += @("-P", $Port, "-r", $LocalPath, $RemoteTarget)

        Write-Host "  -> scp mode: $($attempt.Label)"
        & scp @args
        if ($LASTEXITCODE -eq 0) {
            return
        }

        Write-Warning "scp $($attempt.Label) mode failed for $LocalPath"
    }

    throw "Failed to upload $LocalPath with both default and legacy scp modes"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$remote = "$User@${ServerHost}"
$items = @(
    "app.py",
    "serve.py",
    "runtime_env.py",
    "admin_store.py",
    "desktop_sync.py",
    "feature_access.py",
    "journal_alerts.py",
    "ai.py",
    "alipay.py",
    "zpay.py",
    "membership.py",
    "search.py",
    "build_index.py",
    "site_content.py",
    "requirements.txt",
    "README.md",
    "DEPLOY_SERVER.md",
    "deploy",
    "scripts",
    "config",
    "data",
    "pdfs",
    "static",
    "templates"
)

Write-Host "Creating remote directories under $RemoteDir ..."
& ssh -p $Port $remote "mkdir -p '$RemoteDir'"

foreach ($item in $items) {
    $localPath = Join-Path $repoRoot $item
    if (-not (Test-Path $localPath)) {
        throw "Missing local path: $localPath"
    }

    Write-Host "Uploading $item ..."
    Invoke-Upload -LocalPath $localPath -RemoteTarget "${remote}:$RemoteDir/" -Port $Port
}

Write-Host ""
Write-Host "Upload complete."
Write-Host "Next step on the server:"
Write-Host "  sudo bash $RemoteDir/deploy/bootstrap_ubuntu.sh --domain <your-domain> --zpay-pid '<pid>' --zpay-key '<key>'"
Write-Host "Then configure notify/return URLs in the payment provider dashboard and restart marx-search."
