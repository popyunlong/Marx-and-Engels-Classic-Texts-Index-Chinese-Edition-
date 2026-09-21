[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExpectedLive,
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = $env:MARX_DEPLOY_KEY,
    [switch]$DryRun,
    [switch]$KeepLocalArchive
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$dbPath = Join-Path $repoRoot "data\dictionary.sqlite"
$reportPath = Join-Path $repoRoot "data\dictionary_polish_report.json"
Push-Location $repoRoot
try {
    & python scripts\build_dictionary_index.py --db $dbPath --report $reportPath
    if ($LASTEXITCODE -ne 0) { throw "Dictionary build failed." }
    & python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); n=c.execute('select count(*) from entries').fetchone()[0]; assert n>500,n; print(f'dictionary entries={n}')" $dbPath
    if ($LASTEXITCODE -ne 0) { throw "Dictionary validation failed." }
} finally {
    Pop-Location
}

Write-Warning "The generated dictionary must already be committed and pushed; dirty output will be rejected."
$releaseArguments = @{
    ExpectedLive = $ExpectedLive
    ServerHost = $ServerHost
    ServerUser = $User
    RemoteRoot = $RemoteDir
    SshPort = $Port
    IdentityFile = $IdentityFile
    DryRun = $DryRun
    KeepArtifact = $KeepLocalArchive
}
& (Join-Path $PSScriptRoot "release.ps1") @releaseArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
