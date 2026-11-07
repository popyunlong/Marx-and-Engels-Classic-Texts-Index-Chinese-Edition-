param([Parameter(Mandatory=$true)][ValidateSet('backend','browser','authorized-recheck','new-edges')][string]$Kind)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$outputRoot = 'D:\CodexData\outputs\ai-dialogue-polish-20261106'
$tempRoot = 'D:\CodexData\temp\ai-dialogue-polish-20261106'
New-Item -ItemType Directory -Path $outputRoot,$tempRoot -Force | Out-Null
$recordPath = Join-Path $outputRoot "$Kind-run.json"
if (Test-Path -LiteralPath $recordPath) { throw 'Already attempted. Explicit scenario-specific rerun authorization is required.' }
$env:PYTHON = 'C:\Users\10108\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = "D:\CodexData\dependencies\ai-comprehensive-20261105;D:\CodexData\dependencies\history-opensource-20261031\mineru-env\Lib\site-packages;$taskRoot;$taskRoot\tests"
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:NODE_PATH = 'C:\Users\10108\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
$env:AI_COMPREHENSIVE_OUTPUT = $outputRoot
$versions = @{}
foreach ($file in @('ai.py','ai_quote_variants.py','ai_citations.py','ai_citation_runtime.py','ai_comprehensive.py','comprehensive_web.py','static/ai-page/ai-page.js','static/ai-page/quote-variants.js','static/ai-page/ai-page.css','templates/ai.html','tests/test_dialogue_polish.py','tests/browser/dialogue_polish.cjs')) {
  $versions[$file] = (Get-FileHash -LiteralPath (Join-Path $taskRoot $file) -Algorithm SHA256).Hash
}
$log = Join-Path $outputRoot "$Kind.log"
$run = [ordered]@{task='ai-dialogue-polish-20261106'; id=$Kind; status='running'; started_at=[DateTimeOffset]::Now.ToString('o'); source_sha256=$versions; log=$log; exit_code=$null; summary=''}
$run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath -Encoding utf8
Push-Location -LiteralPath $taskRoot
try {
  if ($Kind -eq 'new-edges') {
    & $env:PYTHON -m pytest tests/test_dialogue_polish_edges.py -q -o "cache_dir=$tempRoot\edges-cache" "--basetemp=$tempRoot\edges" "--junitxml=$outputRoot\edges.xml" 2>&1 | Tee-Object -FilePath $log
  } elseif ($Kind -eq 'authorized-recheck') {
    $run.summary='User authorized only the failed route scenario and four failed browser scenarios. Original results retained.'
    & $env:PYTHON -m pytest tests/test_dialogue_polish.py::test_route_parallel_preparation_and_streamed_simple_result -q -o "cache_dir=$tempRoot\recheck-cache" "--basetemp=$tempRoot\recheck" "--junitxml=$outputRoot\authorized-route.xml" 2>&1 | Tee-Object -FilePath $log
    $routeExit=$LASTEXITCODE
    $env:AI_COMPREHENSIVE_OUTPUT=Join-Path $outputRoot 'authorized-browser'
    New-Item -ItemType Directory -Path $env:AI_COMPREHENSIVE_OUTPUT -Force | Out-Null
    & node --test tests/browser/dialogue_polish.cjs 2>&1 | Tee-Object -FilePath (Join-Path $outputRoot 'authorized-browser.log')
    if ($routeExit -ne 0) { $global:LASTEXITCODE=$routeExit }
  } elseif ($Kind -eq 'backend') {
    & $env:PYTHON -m pytest tests/test_dialogue_polish.py -q -o "cache_dir=$tempRoot\pytest-cache" "--basetemp=$tempRoot\pytest" "--junitxml=$outputRoot\backend.xml" 2>&1 | Tee-Object -FilePath $log
  } else {
    & node --test tests/browser/dialogue_polish.cjs 2>&1 | Tee-Object -FilePath $log
  }
  $run.exit_code=$LASTEXITCODE
  $run.status=if($LASTEXITCODE -eq 0){'passed'}else{'failed'}
} catch { $run.status='interrupted'; $run.summary=$_.ToString(); throw }
finally {
  $run.finished_at=[DateTimeOffset]::Now.ToString('o')
  $run | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath -Encoding utf8
  Pop-Location
}
exit $run.exit_code
