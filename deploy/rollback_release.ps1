[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExpectedCurrent,
    [Parameter(Mandatory = $true)][string]$TargetRelease,
    [string]$ServerHost = "38.76.174.234",
    [string]$ServerUser = "root",
    [string]$RemoteRoot = "/opt/marx-search",
    [int]$SshPort = 22,
    [string]$IdentityFile = $env:MARX_DEPLOY_KEY,
    [ValidateSet('main','mylib-ocr')][string]$Component = 'main',
    [int]$PreviousMainPid = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($Component -eq 'mylib-ocr') {
    $ocrHost = if ($PSBoundParameters.ContainsKey('ServerHost')) { $ServerHost } else { '154.201.94.174' }
    & (Join-Path $PSScriptRoot 'mylib_ocr_component.ps1') -Operation rollback -Expected $ExpectedCurrent -TargetRelease $TargetRelease -ServerHost $ocrHost -ServerUser $ServerUser -SshPort $SshPort -IdentityFile $IdentityFile -PreviousMainPid $PreviousMainPid
    return
}
foreach ($value in @($ExpectedCurrent, $TargetRelease)) {
    if ($value -notmatch '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,191}$') {
        throw "Release ids contain unsafe characters."
    }
}
if ($RemoteRoot -notmatch '^/[A-Za-z0-9._/-]+$') { throw "Unsafe RemoteRoot." }
$ssh = (Get-Command ssh -ErrorAction Stop).Source
$argsList = @("-p", "$SshPort", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes")
if ($IdentityFile) {
    $identity = [IO.Path]::GetFullPath($IdentityFile)
    if (-not (Test-Path -LiteralPath $identity -PathType Leaf)) { throw "Identity file not found: $identity" }
    $argsList += @("-i", $identity)
}
$remote = "${ServerUser}@${ServerHost}"
$command = "bash '$RemoteRoot/current/app/deploy/rollback_release.sh' '$RemoteRoot' '$ExpectedCurrent' '$TargetRelease'"
& $ssh @argsList $remote $command
if ($LASTEXITCODE -ne 0) { throw "Audited rollback failed with exit code $LASTEXITCODE." }
