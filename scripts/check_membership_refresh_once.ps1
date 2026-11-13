param([Parameter(Mandatory=$true)][ValidateSet('static','backend','browser','sample-authorized-retry','static-authorized-retry','edges','mcp-authorized-retry','compatibility','denial-authorized-retry')][string]$Stage)
$ErrorActionPreference='Stop'
$repo=Split-Path $PSScriptRoot -Parent
$output='D:/CodexData/outputs/membership-refresh-20261113'
$temp='D:/CodexData/temp/membership-refresh-20261113'
New-Item -ItemType Directory -Force $output,$temp | Out-Null
$record=Join-Path $output "$Stage.json"
if(Test-Path -LiteralPath $record){throw 'Already attempted. Explicit scope-specific rerun authorization is required.'}
$env:PYTHON='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1112-611-0_26.1112.611.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$env:PYTHONPATH="D:/CodexData/dependencies/ai-comprehensive-20261105;D:/CodexData/dependencies/history-opensource-20261031/mineru-env/Lib/site-packages;D:/CodexData/dependencies/agent-api-mcp-20261107/Lib/site-packages;$repo;$repo/tests"
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'
$env:TEMP=$temp;$env:TMP=$temp
$env:MARX_SKIP_STARTUP_MAINTENANCE='1'
$env:NODE_PATH='C:/Users/10108/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
$env:BROWSER_CHANNEL='chrome'
$env:MEMBERSHIP_REFRESH_OUTPUT=$output
$env:MEMBERSHIP_MCP_PYTHON='D:/CodexData/dependencies/agent-api-mcp-20261107/Scripts/python.exe'
Push-Location -LiteralPath $repo
$files=@(git -c core.safecrlf=false diff --name-only)+@(git ls-files --others --exclude-standard)
$hashes=@{}
foreach($file in $files){if(Test-Path -LiteralPath $file){$hashes[$file]=(Get-FileHash -LiteralPath $file).Hash}}
$run=@{status='running_or_interrupted';stage=$Stage;baseline=(git rev-parse HEAD);hashes=$hashes;started=[DateTimeOffset]::UtcNow.ToString('o');log="$output/$Stage.log"}
$run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $record -Encoding utf8
$code=1
try{
 if($Stage -eq 'sample-authorized-retry'){
  $run.authorization='User explicitly approved sample boundary test and static syntax once on 2026-11-13'
  & $env:PYTHON -m pytest tests/test_membership_refresh.py::test_sample_fixed_twelve_and_direct_url_authorization -q -o "cache_dir=$temp/sample-cache" "--basetemp=$temp/sample-retry" "--junitxml=$output/sample-retry.xml" *> $run.log
  $code=$LASTEXITCODE
 }elseif($Stage -eq 'mcp-authorized-retry'){
  $run.authorization='Scope-specific MCP cancellation retry after runtime correction'
  & $env:PYTHON -m pytest tests/test_membership_refresh_edges.py::test_mcp_cancel_forwards_owner_identity_to_shared_control -q -o "cache_dir=$temp/mcp-cache" "--basetemp=$temp/mcp-retry" "--junitxml=$output/mcp-retry.xml" *> $run.log
  $code=$LASTEXITCODE
 }elseif($Stage -eq 'denial-authorized-retry'){
  $run.authorization='Scope-specific user denial compatibility retry'
  & $env:PYTHON -m pytest tests/test_security.py::SecurityRegressionTests::test_member_user_permission_denies_library_access -q -o "cache_dir=$temp/denial-cache" "--basetemp=$temp/denial-retry" "--junitxml=$output/denial-retry.xml" *> $run.log
  $code=$LASTEXITCODE
 }elseif($Stage -eq 'compatibility'){
  & $env:PYTHON -m pytest tests/test_security.py::SecurityRegressionTests::test_zpay_notify_rejects_param_tamper_and_is_idempotent tests/test_security.py::SecurityRegressionTests::test_pending_orders_are_reused_and_duplicate_pending_orders_are_expired tests/test_security.py::SecurityRegressionTests::test_member_user_permission_denies_library_access tests/test_security.py::SecurityRegressionTests::test_ai_locked_pages_do_not_show_default_model tests/test_member_web_20261112.py::test_membership_daily_quota tests/test_legacy_promise.py::test_mixed_new_plan_fallback_and_pro_authorization tests/test_legacy_promise.py::test_new_wallet_pays_after_twenty_gifts_without_weekly_charge -q -o "cache_dir=$temp/compatibility-cache" "--basetemp=$temp/compatibility" "--junitxml=$output/compatibility.xml" *> $run.log
  $code=$LASTEXITCODE
 }elseif($Stage -eq 'edges'){
  & $env:PYTHON -m pytest tests/test_membership_refresh_edges.py -q -o "cache_dir=$temp/edges-cache" "--basetemp=$temp/edges" "--junitxml=$output/edges.xml" *> $run.log
  $code=$LASTEXITCODE
 }elseif($Stage -eq 'backend'){
  & $env:PYTHON -m pytest tests/test_membership_refresh.py -q -o "cache_dir=$temp/pytest-cache" "--basetemp=$temp/pytest" "--junitxml=$output/backend.xml" *> $run.log
  $code=$LASTEXITCODE
 }elseif($Stage -eq 'browser'){
  node --test tests/browser/membership_refresh.cjs *> $run.log
  $code=$LASTEXITCODE
 }else{
  if($Stage -eq 'static-authorized-retry'){$run.authorization='User explicitly approved sample boundary test and static syntax once on 2026-11-13'}
  & $env:PYTHON scripts/check_membership_refresh_syntax.py *> $run.log
  $code=$LASTEXITCODE
 }
 $run.exit_code=$code;$run.status=if($code -eq 0){'passed'}else{'failed'}
}catch{
 $run.status='interrupted';$_ | Out-String | Add-Content -LiteralPath $run.log
}finally{
 $run.finished=[DateTimeOffset]::UtcNow.ToString('o')
 $run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $record -Encoding utf8
 Pop-Location
}
Get-Content -LiteralPath $run.log -Tail 55
exit $code
