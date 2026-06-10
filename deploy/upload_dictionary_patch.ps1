param(
    [string]$ServerHost = "38.76.174.234",
    [string]$User = "root",
    [string]$RemoteDir = "/opt/marx-search",
    [int]$Port = 22,
    [string]$IdentityFile = "$HOME\.ssh\id_marx_cloud_ed25519",
    [switch]$DryRun,
    [switch]$SkipRestart,
    [switch]$KeepLocalArchive,
    [switch]$UploadPdfs,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    Write-Host $Label
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$dbPath = Join-Path $repoRoot "data\dictionary.sqlite"
$reportPath = Join-Path $repoRoot "data\dictionary_polish_report.json"

Push-Location $repoRoot
try {
    Invoke-Checked -Label "Building and polishing dictionary index ..." -FilePath "python" -ArgumentList @(
        "scripts\build_dictionary_index.py",
        "--db", $dbPath,
        "--report", $reportPath
    )
    Invoke-Checked -Label "Validating dictionary SQLite ..." -FilePath "python" -ArgumentList @(
        "-c",
        "import sqlite3,sys; db=sys.argv[1]; c=sqlite3.connect(db); n=c.execute('select count(*) from entries').fetchone()[0]; bad=c.execute('select count(*) from entries where instr(content, ?) > 0 or instr(content, ?) > 0 or instr(content, ?) > 0', (chr(0xfffd), chr(0x22ef)*2, '??')).fetchone()[0]; assert n>500, n; assert bad==0, bad; print(f'dictionary entries={n}')",
        $dbPath
    )
} finally {
    Pop-Location
}

$updateScript = Join-Path $PSScriptRoot "update_cloud.ps1"
if (-not (Test-Path $updateScript)) {
    throw "Missing unified cloud patch script: $updateScript"
}

$argsList = @(
    "-ExecutionPolicy", "Bypass",
    "-NoProfile",
    "-File", $updateScript,
    "-ServerHost", $ServerHost,
    "-User", $User,
    "-RemoteDir", $RemoteDir,
    "-Port", $Port,
    "-IdentityFile", $IdentityFile
)

if ($SkipRestart) { $argsList += "-SkipRestart" }
if ($DryRun) { $argsList += "-DryRun" }
if ($KeepLocalArchive) { $argsList += "-KeepLocalArchive" }
if ($AllowDirty) { $argsList += "-AllowDirty" }

& powershell @argsList
if ($LASTEXITCODE -ne 0) {
    throw "Unified cloud patch script failed."
}

if ($UploadPdfs) {
    Write-Warning "Dictionary PDF upload is intentionally not bundled in the compact patch. Upload the two source PDFs separately only when the server must rebuild the index."
}
