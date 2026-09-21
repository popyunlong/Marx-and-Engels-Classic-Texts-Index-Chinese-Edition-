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

Write-Warning "Reader-only incremental patches are retired; a complete immutable release will be built."
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
