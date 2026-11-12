param([Parameter(Mandatory=$true)][ValidateSet('backend','cutover','browser')][string]$Stage)
$ErrorActionPreference='Stop'
$repo=Split-Path $PSScriptRoot -Parent
$output='D:/CodexData/outputs/legacy-promise-20261112'
$temp='D:/CodexData/temp/legacy-promise-20261112'
$record=Join-Path $output "$Stage.json"
if(Test-Path -LiteralPath $record){throw 'Already attempted: specific retest authorization is required.'}
New-Item -ItemType Directory -Force $output,$temp | Out-Null
$env:PYTHON='C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONPATH="D:/CodexData/dependencies/ai-comprehensive-20261105;D:/CodexData/dependencies/history-opensource-20261031/mineru-env/Lib/site-packages;$repo;$repo/tests"
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'
$env:TEMP=$temp
$env:TMP=$temp
$env:MARX_RUNTIME_LOG_DIR="$temp/logs"
$env:NODE_PATH='C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
$env:BROWSER_CHANNEL='chrome'
$env:AI_COMPREHENSIVE_OUTPUT=$output
Push-Location -LiteralPath $repo
$hashes=@{}
foreach($file in (@(git diff --name-only)+@(git ls-files --others --exclude-standard))){
    if(Test-Path -LiteralPath $file -PathType Leaf){$hashes[$file]=(Get-FileHash -LiteralPath $file).Hash}
}
$run=@{status='running';baseline=(git rev-parse HEAD);stage=$Stage;hashes=$hashes;started=[DateTimeOffset]::UtcNow.ToString('o');log="$output/$Stage.log"}
$run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $record -Encoding utf8
try{
    if($Stage -ne 'browser'){
        $targets=if($Stage -eq 'cutover'){
            @('tests/test_legacy_promise.py::test_old_inflight_reservation_settles_after_qualification_change',
              'tests/test_legacy_promise.py::test_late_settlement_after_membership_expiry_keeps_site_cost')
        }else{@('tests/test_legacy_promise.py')}
        $run.scenario=$targets
        & $env:PYTHON -m pytest @targets -q -o "cache_dir=$temp/pytest-cache" "--basetemp=$temp/$Stage" "--junitxml=$output/$Stage.xml" *> $run.log
    }else{
        $run.scenario='tests/browser/legacy_promise.cjs (new protected member browser case only)'
        node --test tests/browser/legacy_promise.cjs *> $run.log
    }
    $run.exit_code=$LASTEXITCODE
    $run.status=if($LASTEXITCODE -eq 0){'passed'}else{'failed'}
}catch{
    $run.exit_code=1
    $run.status='interrupted'
    $_ | Out-String | Add-Content -LiteralPath $run.log
}finally{
    $run.finished=[DateTimeOffset]::UtcNow.ToString('o')
    $run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $record -Encoding utf8
    Pop-Location
}
Get-Content -LiteralPath $run.log -Tail 45
exit $run.exit_code
