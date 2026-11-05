param([Parameter(Mandatory=$true)][ValidateSet('backend','browser','stop','authorized-recheck')][string]$Kind)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$outputRoot = 'D:\CodexData\outputs\ai-comprehensive-20261105'
$tempRoot = 'D:\CodexData\temp\ai-comprehensive-20261105'
$ledgerPath = Join-Path $taskRoot 'docs\ops\AI_COMPREHENSIVE_TEST_LEDGER_20261105.json'
$ledger = Get-Content -LiteralPath $ledgerPath -Raw | ConvertFrom-Json
if (@($ledger.runs | Where-Object { $_.id -eq $Kind }).Count) { throw "Already attempted: $Kind. Explicit user approval is required before rerunning." }
New-Item -ItemType Directory -Path $outputRoot,$tempRoot -Force | Out-Null
$env:PYTHON = 'C:\Users\10108\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = "D:\CodexData\dependencies\ai-comprehensive-20261105;D:\CodexData\dependencies\history-opensource-20261031\mineru-env\Lib\site-packages;$taskRoot;$taskRoot\tests"
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:NODE_PATH = 'C:\Users\10108\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
$env:BROWSER_CHANNEL = 'chrome'
$env:AI_COMPREHENSIVE_OUTPUT = $outputRoot
$versionFiles = @('ai.py','app.py','membership.py','ai_web.py','ai_comprehensive.py','comprehensive_web.py','static/ai-page/ai-page.js','templates/ai.html')
$versions = @{}
foreach ($file in $versionFiles) { $versions[$file] = (Get-FileHash -LiteralPath (Join-Path $taskRoot $file) -Algorithm SHA256).Hash }
$log = Join-Path $outputRoot "$Kind.log"
$run = [pscustomobject]@{ id=$Kind; started_at=[DateTimeOffset]::UtcNow.ToString('o'); status='running'; source_sha256=$versions; log=$log; exit_code=$null }
$ledger.runs = @($ledger.runs) + $run
$ledger | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ledgerPath -Encoding utf8
Push-Location -LiteralPath $taskRoot
try {
  if ($Kind -eq 'authorized-recheck') {
    # Explicit user consent in this chat: three failed browser scenarios and one prior history scenario only.
    & $env:PYTHON -m pytest tests/test_comprehensive_mode.py::test_long_history_preserves_constraints_headings_and_tail tests/test_comprehensive_mode.py::test_full_context_article_keeps_earlier_requirements_and_reference_locators -q -o "cache_dir=$tempRoot\recheck-cache" "--basetemp=$tempRoot\recheck" "--junitxml=$outputRoot\history-recheck.xml" 2>&1 | Tee-Object -FilePath $log
    $historyExit = $LASTEXITCODE
    & node --test '--test-name-pattern=comprehensive defaults|Word export' tests/browser/comprehensive.cjs 2>&1 | Tee-Object -FilePath (Join-Path $outputRoot 'browser-authorized-recheck.log')
    if ($historyExit -ne 0) { $global:LASTEXITCODE = $historyExit }
  } elseif ($Kind -eq 'backend') {
    & $env:PYTHON -m pytest tests/test_comprehensive_mode.py tests/test_associative_search.py tests/test_web_search_filter.py tests/test_research_citation_recovery.py tests/test_ai_quota_reset.py tests/test_membership_tier.py tests/test_citation_scope_limit.py tests/test_flash_compatibility.py -q -o "cache_dir=$tempRoot\pytest-cache" "--basetemp=$tempRoot\pytest" "--junitxml=$outputRoot\backend.xml" 2>&1 | Tee-Object -FilePath $log
  } elseif ($Kind -eq 'browser') {
    & node --test tests/browser/comprehensive.cjs tests/browser/ai_controls.cjs 2>&1 | Tee-Object -FilePath $log
  } else {
    & node tests/ai_stop_harness.cjs 2>&1 | Tee-Object -FilePath $log
  }
  $run.exit_code = $LASTEXITCODE
  $run.status = if ($LASTEXITCODE -eq 0) { 'passed' } else { 'failed' }
} catch {
  $run.status = 'interrupted'
  $_ | Out-String | Add-Content -LiteralPath $log
  throw
} finally {
  Pop-Location
  $ledger | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ledgerPath -Encoding utf8
}
exit $run.exit_code
