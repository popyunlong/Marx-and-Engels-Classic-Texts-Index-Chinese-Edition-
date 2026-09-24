[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ExpectedLive,

    [string]$ServerHost = "38.76.174.234",
    [string]$ServerUser = "root",
    [string]$RemoteRoot = "/opt/marx-search",
    [int]$SshPort = 22,
    [string]$IdentityFile = $env:MARX_DEPLOY_KEY,
    [string]$ArtifactDirectory = "",
    [string]$CatalogArchive = "",
    [switch]$DryRun,
    [switch]$KeepArtifact
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Missing required command: $Name"
    }
    return $command.Source
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$Capture
    )
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Capture) {
            $output = & $FilePath @ArgumentList 2>&1
        } else {
            & $FilePath @ArgumentList
            $output = @()
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPreference
    }
    if ($exitCode -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw "$Label failed with exit code $exitCode. $detail"
    }
    if ($Capture) {
        return ($output | Out-String).Trim()
    }
}

function Find-Python310 {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $version = (& $launcher.Source -3.10 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq "3.10") {
            return @{ Exe = $launcher.Source; Prefix = @("-3.10") }
        }
    }
    foreach ($name in @("python3.10", "python")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $version = (& $command.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq "3.10") {
            return @{ Exe = $command.Source; Prefix = @() }
        }
    }
    throw "Python 3.10 is required to build a production release. The local Python 3.14 runtime is not accepted."
}

function Invoke-Python310 {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Python,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Invoke-Checked -Label $Label -FilePath $Python.Exe -ArgumentList @($Python.Prefix + $Arguments)
}

if ($ExpectedLive -notmatch '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,191}$') {
    throw "ExpectedLive contains unsafe characters. Copy the exact current release id from /api/runtime or DEPLOYED_SHA."
}
if ($RemoteRoot -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteRoot must be an absolute path containing only safe path characters."
}

$git = Require-Command "git"
$python = Find-Python310
$repoRoot = Invoke-Checked -Label "Locate Git repository" -FilePath $git -ArgumentList @("rev-parse", "--show-toplevel") -Capture
Push-Location $repoRoot
$scratch = $null
$artifactPath = $null
$remoteArchive = $null
$uploaded = $false
$remoteCatalogArchive = ""
try {
    $branch = Invoke-Checked -Label "Read current branch" -FilePath $git -ArgumentList @("symbolic-ref", "--quiet", "--short", "HEAD") -Capture
    if ($branch -ne "production") {
        throw "Production releases may only be built from the production branch; current branch is '$branch'."
    }

    $dirty = Invoke-Checked -Label "Inspect working tree" -FilePath $git -ArgumentList @("status", "--porcelain=v1", "--untracked-files=all") -Capture
    if ($dirty) {
        throw "Refusing to release a dirty working tree. Commit, discard, or move every tracked and untracked change first."
    }

    Invoke-Checked -Label "Refresh protected release refs" -FilePath $git -ArgumentList @("fetch", "--no-tags", "origin", "main", "production")
    $head = Invoke-Checked -Label "Read HEAD" -FilePath $git -ArgumentList @("rev-parse", "HEAD") -Capture
    $remoteHead = Invoke-Checked -Label "Read origin/production" -FilePath $git -ArgumentList @("rev-parse", "origin/production") -Capture
    if ($head -ne $remoteHead) {
        throw "HEAD ($head) is not exactly origin/production ($remoteHead). Push or synchronize before releasing."
    }
    Invoke-Checked -Label "Verify production commit is already integrated in origin/main" -FilePath $git -ArgumentList @(
        "merge-base", "--is-ancestor", $head, "origin/main"
    )

    $utcStamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $builtAt = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    $nonce = ([Guid]::NewGuid().ToString("N")).Substring(0, 8)
    $releaseId = "$head-$utcStamp-$nonce"
    $scratch = Join-Path ([IO.Path]::GetTempPath()) "marx-release-$nonce"
    $appDir = Join-Path $scratch "app"
    New-Item -ItemType Directory -Path $appDir -Force | Out-Null

    $sourceZip = Join-Path $scratch "source.zip"
    Invoke-Checked -Label "Export committed source" -FilePath $git -ArgumentList @("archive", "--format=zip", "--output=$sourceZip", $head)
    Expand-Archive -LiteralPath $sourceZip -DestinationPath $appDir -Force
    Remove-Item -LiteralPath $sourceZip -Force

    $manifestTool = Join-Path $appDir "scripts\build_release_manifest.py"
    $releaseJson = Join-Path $scratch "release.json"
    Invoke-Python310 -Python $python -Label "Create release metadata" -Arguments @(
        $manifestTool, "create",
        "--source-dir", $appDir,
        "--output", $releaseJson,
        "--release-id", $releaseId,
        "--git-sha", $head,
        "--parent-release-id", $ExpectedLive,
        "--built-at", $builtAt
    )
    Invoke-Python310 -Python $python -Label "Verify release metadata" -Arguments @(
        $manifestTool, "verify",
        "--source-dir", $appDir,
        "--metadata", $releaseJson,
        "--release-id", $releaseId,
        "--git-sha", $head
    )
    $oldPycachePrefix = $env:PYTHONPYCACHEPREFIX
    $env:PYTHONPYCACHEPREFIX = Join-Path $scratch "pycache"
    try {
        Invoke-Python310 -Python $python -Label "Compile committed Python source" -Arguments @("-m", "compileall", "-q", $appDir)
    } finally {
        $env:PYTHONPYCACHEPREFIX = $oldPycachePrefix
    }
    Invoke-Python310 -Python $python -Label "Reverify source after compilation" -Arguments @(
        $manifestTool, "verify",
        "--source-dir", $appDir,
        "--metadata", $releaseJson,
        "--release-id", $releaseId,
        "--git-sha", $head
    )

    if ($ArtifactDirectory) {
        $resolvedArtifactDirectory = [IO.Path]::GetFullPath($ArtifactDirectory)
        New-Item -ItemType Directory -Path $resolvedArtifactDirectory -Force | Out-Null
    } else {
        $resolvedArtifactDirectory = [IO.Path]::GetTempPath()
    }
    $artifactPath = Join-Path $resolvedArtifactDirectory "marx-search-$releaseId.tar.gz"
    $archiveTool = Join-Path $appDir "scripts\build_release_archive.py"
    Invoke-Python310 -Python $python -Label "Build deterministic immutable release archive" -Arguments @(
        $archiveTool,
        "--source-dir", $appDir,
        "--metadata", $releaseJson,
        "--output", $artifactPath
    )
    Write-Host "Release archive verified: $artifactPath"
    Write-Host "Release id: $releaseId"
    Write-Host "Parent release id: $ExpectedLive"
    if ($CatalogArchive) {
        $CatalogArchive = (Resolve-Path -LiteralPath $CatalogArchive).Path
        if (-not (Test-Path -LiteralPath (Join-Path $appDir "config/catalog_release.json"))) {
            throw "Catalogue artifacts require a committed config/catalog_release.json binding."
        }
    }

    if ($DryRun) {
        $KeepArtifact = $true
        Write-Host "Dry run complete; no server connection was made."
        return
    }

    $ssh = Require-Command "ssh"
    $scp = Require-Command "scp"
    $sshCommon = @("-p", "$SshPort", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes")
    $scpCommon = @("-P", "$SshPort", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes")
    if ($IdentityFile) {
        $identity = [IO.Path]::GetFullPath($IdentityFile)
        if (-not (Test-Path -LiteralPath $identity -PathType Leaf)) {
            throw "Identity file does not exist: $identity"
        }
        $sshCommon += @("-i", $identity)
        $scpCommon += @("-i", $identity)
    }
    $remoteArchive = "/var/tmp/marx-search-$releaseId.tar.gz"
    $remote = "${ServerUser}@${ServerHost}"
    Invoke-Checked -Label "Upload unique release archive" -FilePath $scp -ArgumentList @($scpCommon + @($artifactPath, "${remote}:$remoteArchive"))
    $uploaded = $true

    if ($CatalogArchive) {
        $remoteCatalogArchive = "/var/tmp/marx-catalog-$releaseId.tar.gz"
        Invoke-Checked -Label "Upload bound catalogue artifact" -FilePath $scp -ArgumentList @($scpCommon + @($CatalogArchive, "${remote}:$remoteCatalogArchive"))
    }

    $reviewNonce = [Guid]::NewGuid().ToString("N")
    $remoteCommand = "tar -xOf '$remoteArchive' app/deploy/promote_release.sh | bash -s -- '$RemoteRoot' '$remoteArchive' '$ExpectedLive' '$releaseId' '$remoteCatalogArchive' '$reviewNonce'"
    Write-Host "Candidate review nonce: $reviewNonce"
    Write-Host "The transaction will pause before cutover for candidate browser checks."
    Invoke-Checked -Label "Promote release transaction" -FilePath $ssh -ArgumentList @($sshCommon + @($remote, $remoteCommand))
    Write-Host "Production release completed: $releaseId"
} finally {
    if ($uploaded -and $remoteArchive) {
        try {
            $ssh = Get-Command ssh -ErrorAction SilentlyContinue
            if ($ssh) {
                $cleanupArgs = @("-p", "$SshPort", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes")
                if ($IdentityFile) { $cleanupArgs += @("-i", [IO.Path]::GetFullPath($IdentityFile)) }
                $cleanupArgs += @("${ServerUser}@${ServerHost}", "rm -f -- '$remoteArchive'")
                & $ssh.Source @cleanupArgs 2>$null | Out-Null
                if ($remoteCatalogArchive) {
                    $cleanupArgs[-1] = "rm -f -- '$remoteCatalogArchive'"
                    & $ssh.Source @cleanupArgs 2>$null | Out-Null
                }
            }
        } catch {
            Write-Warning "Could not remove the unique remote upload: $remoteArchive"
        }
    }
    if ($scratch -and (Test-Path -LiteralPath $scratch)) {
        Remove-Item -LiteralPath $scratch -Recurse -Force
    }
    if ($artifactPath -and -not $KeepArtifact -and (Test-Path -LiteralPath $artifactPath)) {
        Remove-Item -LiteralPath $artifactPath -Force
    }
    Pop-Location
}
