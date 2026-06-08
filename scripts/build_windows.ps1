$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
Set-Location $projectRoot

function Invoke-BuildPython {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]] $Args
    )

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $installed = cmd /c "py -0p 2>nul" | Out-String
        foreach ($version in @("-3.10", "-3.11")) {
            if ($installed -match [regex]::Escape($version)) {
                & py $version @Args
                return
            }
        }
        & py @Args
        return
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python @Args
        return
    }

    throw "Python launcher not found. Install Python 3.10+ first."
}

$dataVersion = Get-Date -Format "yyyy.MM.dd"

Invoke-BuildPython -m pip install --upgrade pip
Invoke-BuildPython -m pip install -r requirements.txt -r requirements-build.txt
Invoke-BuildPython scripts/write_release_metadata.py --data-dir data --data-version $dataVersion
Invoke-BuildPython -m PyInstaller --noconfirm --clean app.spec

$exeArtifact = Get-ChildItem (Join-Path $projectRoot "dist") -Filter "*.exe" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $exeArtifact) {
    throw "Build succeeded but no .exe artifact was found in dist."
}

$releaseRoot = Join-Path $projectRoot ("release\windows-full-" + $dataVersion)
if (Test-Path $releaseRoot) {
    Remove-Item -LiteralPath $releaseRoot -Recurse -Force
}

$programDir = Join-Path $releaseRoot "program"
$assetsDir = Join-Path $releaseRoot "assets"
New-Item -ItemType Directory -Force -Path $programDir, $assetsDir | Out-Null

Copy-Item -LiteralPath $exeArtifact.FullName -Destination $programDir
Copy-Item -LiteralPath (Join-Path $projectRoot "config") -Destination $assetsDir -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "data") -Destination $assetsDir -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "pdfs") -Destination $assetsDir -Recurse

Write-Host ""
Write-Host "Windows full-edition files ready:"
Write-Host $releaseRoot
