param([ValidateSet('syntax','backend','browser','authorized-recheck','transport-edges','browser-edges','control-tail','resume-edges','scope-recheck')][string]$Kind)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot -Parent
$outputRoot='D:\CodexData\outputs\ai-dialogue-lifecycle-fix-20261107'
$tempRoot='D:\CodexData\temp\ai-dialogue-lifecycle-fix-20261107'
New-Item -ItemType Directory -Path $outputRoot,$tempRoot -Force | Out-Null
$record=Join-Path $outputRoot "$Kind.json"
if(Test-Path -LiteralPath $record){throw 'Already attempted; explicit new rerun authorization required.'}
$env:PYTHON='C:\Users\10108\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH="D:\CodexData\dependencies\ai-comprehensive-20261105;D:\CodexData\dependencies\history-opensource-20261031\mineru-env\Lib\site-packages;$taskRoot;$taskRoot\tests"
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'
$env:TEMP=$tempRoot
$env:TMP=$tempRoot
$env:NODE_PATH='C:\Users\10108\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
$files=@('ai.py','ai_comprehensive.py','ai_generation_flow.py','ai_generation_protocol.py','comprehensive_web.py','static/ai-page/ai-page.js','templates/ai.html','tests/test_dialogue_lifecycle.py','tests/test_comprehensive_mode.py','tests/test_dialogue_polish.py','tests/browser/dialogue_lifecycle.cjs','tests/browser/serve_ai_fixture.py','tests/test_dialogue_transport_edges.py','tests/browser/dialogue_lifecycle_edges.cjs','tests/test_dialogue_control_tail.py','tests/dialogue_deletion_recovery.cjs','tests/test_dialogue_resume_edges.py')
$hashes=@{}
foreach($file in $files){$hashes[$file]=(Get-FileHash -LiteralPath (Join-Path $taskRoot $file)).Hash}
$run=[ordered]@{kind=$Kind;baseline='d848df34df3056a54b245fe666da9827e71c6eeb';authorization='同意上述最小范围各复测一次';started=[DateTimeOffset]::Now.ToString('o');status='running';hashes=$hashes;log=(Join-Path $outputRoot "$Kind.log")}
$run | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $record -Encoding utf8
Push-Location $taskRoot
try {
 if($Kind -eq 'syntax'){
  & $env:PYTHON -B -c "import ast,pathlib; paths=['ai.py','ai_comprehensive.py','ai_generation_flow.py','ai_generation_protocol.py','comprehensive_web.py','tests/test_dialogue_lifecycle.py','tests/test_comprehensive_mode.py','tests/browser/serve_ai_fixture.py']; [ast.parse(pathlib.Path(p).read_text(encoding='utf-8'),filename=p) for p in paths]; print('Python syntax: passed')" 2>&1 | Tee-Object -FilePath $run.log
  if($LASTEXITCODE -ne 0){throw 'Python syntax failed'}
  & node --check static/ai-page/ai-page.js 2>&1 | Tee-Object -FilePath $run.log -Append
  if($LASTEXITCODE -ne 0){throw 'Frontend syntax failed'}
  & node --check tests/browser/dialogue_lifecycle.cjs 2>&1 | Tee-Object -FilePath $run.log -Append
 } elseif($Kind -eq 'backend'){
  & $env:PYTHON -m pytest tests/test_dialogue_lifecycle.py tests/test_comprehensive_mode.py tests/test_dialogue_polish.py -q -o "cache_dir=$tempRoot\pytest-cache" "--basetemp=$tempRoot\pytest" "--junitxml=$outputRoot\backend.xml" 2>&1 | Tee-Object -FilePath $run.log
 } elseif($Kind -eq 'authorized-recheck'){
  & $env:PYTHON -B -c "import ast,pathlib; [ast.parse(pathlib.Path(p).read_text(encoding='utf-8'),filename=p) for p in ['ai.py','tests/test_comprehensive_mode.py']]; print('authorized syntax: passed')" 2>&1 | Tee-Object -FilePath $run.log
  if($LASTEXITCODE -ne 0){throw 'Authorized syntax failed'}
  & $env:PYTHON -m pytest tests/test_comprehensive_mode.py::test_route_continuation_reuses_plan_and_completed_sections -q -o "cache_dir=$tempRoot\recheck-cache" "--basetemp=$tempRoot\recheck" "--junitxml=$outputRoot\authorized-recheck.xml" 2>&1 | Tee-Object -FilePath $run.log -Append
 } elseif($Kind -eq 'transport-edges'){
  & $env:PYTHON -m pytest tests/test_dialogue_transport_edges.py -q -o "cache_dir=$tempRoot\transport-cache" "--basetemp=$tempRoot\transport" "--junitxml=$outputRoot\transport-edges.xml" 2>&1 | Tee-Object -FilePath $run.log
 } elseif($Kind -eq 'scope-recheck'){
  & $env:PYTHON -m pytest tests/test_dialogue_resume_edges.py::test_finished_parent_result_cannot_bypass_continuation_scope_check -q -o "cache_dir=$tempRoot\scope-cache" "--basetemp=$tempRoot\scope" "--junitxml=$outputRoot\scope-recheck.xml" 2>&1 | Tee-Object -FilePath $run.log
 } elseif($Kind -eq 'resume-edges'){
  & $env:PYTHON -m pytest tests/test_dialogue_resume_edges.py -q -o "cache_dir=$tempRoot\resume-cache" "--basetemp=$tempRoot\resume" "--junitxml=$outputRoot\resume-edges.xml" 2>&1 | Tee-Object -FilePath $run.log
 } elseif($Kind -eq 'control-tail'){
  & $env:PYTHON -m pytest tests/test_dialogue_control_tail.py -q -o "cache_dir=$tempRoot\control-cache" "--basetemp=$tempRoot\control" "--junitxml=$outputRoot\control-tail.xml" 2>&1 | Tee-Object -FilePath $run.log
 } elseif($Kind -eq 'browser-edges'){
  & node --test tests/browser/dialogue_lifecycle_edges.cjs 2>&1 | Tee-Object -FilePath $run.log
 } else {
  & node --test tests/browser/dialogue_lifecycle.cjs 2>&1 | Tee-Object -FilePath $run.log
 }
 $run.exit_code=$LASTEXITCODE
 $run.status=if($LASTEXITCODE -eq 0){'passed'}else{'failed'}
} catch {$run.status='failed';$run.error=$_.ToString();throw}
finally {$run.finished=[DateTimeOffset]::Now.ToString('o');$run | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $record -Encoding utf8;Pop-Location}
exit $run.exit_code
