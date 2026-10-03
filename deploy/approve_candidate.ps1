[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseId,
    [Parameter(Mandatory = $true)][string]$Nonce,
    [Parameter(Mandatory = $true)][string]$EvidenceFile,
    [switch]$Reject,
    [string]$ServerHost = "38.76.174.234",
    [string]$ServerUser = "root",
    [int]$SshPort = 22,
    [string]$IdentityFile = $env:MARX_DEPLOY_KEY
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($ReleaseId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw "Unsafe release id" }
if ($Nonce -cnotmatch '^[0-9a-f]{32}$') { throw "Unsafe review nonce" }
$evidence = Get-Content -LiteralPath $EvidenceFile -Raw -Encoding utf8 | ConvertFrom-Json
$expectedResult = if ($Reject) { 'fail' } else { 'pass' }
if ($evidence.release_id -cne $ReleaseId -or $evidence.result -cne $expectedResult) {
    throw "Candidate review evidence must identify this release and record $expectedResult"
}
if (-not $evidence.checked_at -or -not $evidence.checks -or $evidence.checks.Count -lt 1) {
    throw "Candidate review evidence is incomplete"
}
$sshArgs = @('-p', "$SshPort", '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes')
if ($IdentityFile) { $sshArgs += @('-i', [IO.Path]::GetFullPath($IdentityFile)) }
$pipe = "/run/marx-search-candidate-$ReleaseId.fifo"
$decision = if ($Reject) { 'FAIL' } else { 'PASS' }
if (-not $Reject) {
    # Validate on the immutable candidate itself; legacy checks cannot approve a
    # new corpus unless every website flow, browser and rollback was exercised.
    $validate = "python3 '/opt/marx-search/releases/$ReleaseId/app/scripts/corpus_deploy.py' review --app '/opt/marx-search/releases/$ReleaseId/app'"
    Get-Content -LiteralPath $EvidenceFile -Raw -Encoding utf8 | & ssh @sshArgs "${ServerUser}@${ServerHost}" $validate
    if ($LASTEXITCODE -ne 0) { throw "Corpus candidate functional evidence was rejected" }
    $validateCatalog = "python3 '/opt/marx-search/releases/$ReleaseId/app/scripts/catalog_review.py' --app '/opt/marx-search/releases/$ReleaseId/app'"
    Get-Content -LiteralPath $EvidenceFile -Raw -Encoding utf8 | & ssh @sshArgs "${ServerUser}@${ServerHost}" $validateCatalog
    if ($LASTEXITCODE -ne 0) { throw "Catalogue candidate observation evidence was rejected" }
}
$remote = "test -p '$pipe' && printf '%s\n' '$ReleaseId`:$Nonce`:$decision' > '$pipe'"
& ssh @sshArgs "${ServerUser}@${ServerHost}" $remote
if ($LASTEXITCODE -ne 0) { throw "Candidate transaction did not accept the review receipt" }
Write-Host "Candidate review $expectedResult recorded for $ReleaseId"
