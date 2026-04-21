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

        foreach ($version in @("-3.11", "-3.10")) {
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

Invoke-BuildPython -m pip install --upgrade pip
Invoke-BuildPython -m pip install -r requirements.txt -r requirements-build.txt
Invoke-BuildPython -m PyInstaller --noconfirm --clean app.spec

$artifact = Get-ChildItem (Join-Path $projectRoot "dist") -Filter "*.exe" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName

if (-not $artifact) {
    throw "Build succeeded but no .exe artifact was found in dist."
}

Write-Host ""
Write-Host "Windows artifact ready:"
Write-Host $artifact
