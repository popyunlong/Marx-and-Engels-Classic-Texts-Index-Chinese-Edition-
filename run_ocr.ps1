# Run "Zhongyao Wenxian Xuanbian" OCR to completion.
# Low priority (idle), auto-resume on crash, exits when all 4 scanned volumes done.
# ASCII-only to stay safe under Windows PowerShell 5.1 (GBK) parsing.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$targets = @(
  @{ f = "data/xuanbian_18_zhong_ocr.jsonl"; n = 850; name = "18da-zhong" },
  @{ f = "data/xuanbian_18_v906_ocr.jsonl";  n = 906; name = "18da-shang" },
  @{ f = "data/xuanbian_19_shang_ocr.jsonl"; n = 906; name = "19da-shang" },
  @{ f = "data/xuanbian_19_zhong_ocr.jsonl"; n = 849; name = "19da-zhong" }
)

function Get-Count($path) {
  if (Test-Path $path) { return (Get-Content $path | Measure-Object -Line).Lines }
  return 0
}
function Show-Progress {
  foreach ($t in $targets) {
    $c = Get-Count $t.f
    Write-Host ("  {0,-12} {1,4} / {2}" -f $t.name, $c, $t.n)
  }
}
function All-Done {
  foreach ($t in $targets) { if ((Get-Count $t.f) -lt $t.n) { return $false } }
  return $true
}

Write-Host "=== Xuanbian OCR runner (low priority, resumable) ==="
Show-Progress
$round = 0
while (-not (All-Done)) {
  $round++
  Write-Host ("[{0}] round {1}: OCR running (idle priority) ..." -f (Get-Date -Format HH:mm:ss), $round)
  python scripts\_ocr_queue.py
  Write-Host ("[{0}] progress:" -f (Get-Date -Format HH:mm:ss))
  Show-Progress
  if (-not (All-Done)) { Start-Sleep -Seconds 5 }
}
Write-Host "=== ALL 4 VOLUMES OCR DONE. You can tell the assistant to continue. ==="
