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

# Compatibility entry point. The former incremental uploader was intentionally
# removed: all application-source releases now use the same immutable,
# compare-and-swap transaction and the same server-side flock.
$release = Join-Path $PSScriptRoot "release.ps1"
$arguments = @{
    ExpectedLive = $ExpectedLive
    ServerHost = $ServerHost
    ServerUser = $User
    RemoteRoot = $RemoteDir
    SshPort = $Port
    IdentityFile = $IdentityFile
    DryRun = $DryRun
    KeepArtifact = $KeepLocalArchive
}
& $release @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
