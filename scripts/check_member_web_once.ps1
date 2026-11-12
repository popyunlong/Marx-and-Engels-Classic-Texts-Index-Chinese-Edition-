param([Parameter(Mandatory=$true)][ValidateSet('backend','edges','browser')][string]$Stage)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$output = 'D:/CodexData/outputs/member-web-20261112'
$temp = 'D:/CodexData/temp/member-web-20261112'
$record = Join-Path $output "$Stage.json"
if (Test-Path -LiteralPath $record) { throw 'This stage was already attempted. Explicit user authorization is required to rerun it.' }
New-Item -ItemType Directory -Force $output,$temp | Out-Null
$env:PYTHON = 'C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONPATH = "D:/CodexData/dependencies/ai-comprehensive-20261105;D:/CodexData/dependencies/history-opensource-20261031/mineru-env/Lib/site-packages;$repo;$repo/tests"
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = $temp
$env:TMP = $temp
$env:NODE_PATH = 'C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
$env:BROWSER_CHANNEL = 'chrome'
$env:AI_COMPREHENSIVE_OUTPUT = $output
Push-Location -LiteralPath $repo
$files = @(git diff --name-only) + @(git ls-files --others --exclude-standard)
$hashes = @{}
foreach ($file in $files) { $hashes[$file] = (Get-FileHash -LiteralPath $file).Hash }
$run = @{status='running';baseline=(git rev-parse HEAD);stage=$Stage;hashes=$hashes;started=[DateTimeOffset]::UtcNow.ToString('o');log="$output/$Stage.log"}
$run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $record -Encoding utf8
try {
    if ($Stage -eq 'browser') {
        $run.scenario = 'tests/browser/comprehensive.cjs'
        node --test tests/browser/comprehensive.cjs *> $run.log
    } else {
        $target = if ($Stage -eq 'edges') { 'tests/test_member_web_edges.py' } else { 'tests/test_member_web_20261112.py' }
        $run.scenario = $target
        & $env:PYTHON -m pytest $target -q -o "cache_dir=$temp/pytest-cache" "--junitxml=$output/$Stage.xml" *> $run.log
    }
    $run.exit_code = $LASTEXITCODE
    $run.status = if ($LASTEXITCODE -eq 0) { 'passed' } else { 'failed' }
} catch {
    $run.status = 'interrupted'
    $_ | Out-String | Add-Content -LiteralPath $run.log
} finally {
    $run.finished = [DateTimeOffset]::UtcNow.ToString('o')
    $run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $record -Encoding utf8
    Pop-Location
}
Get-Content -LiteralPath $run.log -Tail 35
exit $run.exit_code
