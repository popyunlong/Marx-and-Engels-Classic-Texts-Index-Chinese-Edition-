param(
    [string]$Python = "python",
    [int]$Workers = 6,
    [int]$FallbackThreads = 2,
    [string]$Only = "",
    [switch]$SkipFallback
)

# 新增习近平专题论述的可恢复批处理：GLM 逐页忠实转录，随后只把拒答、内容过滤、
# 截断、疑似幻觉和误判空白页交给 RapidOCR 兜底。GLM sidecar 可续跑，重复执行安全。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$series = @(
    "xi_lingdao",
    "xi_xuanchuan",
    "xi_dangshi",
    "xi_xinfazhan",
    "xi_ziwogeming",
    "zb_qunzhong",
    "zb_anquan",
    "zb_wangluo",
    "zb_jingshen",
    "zb_zhengji"
)
if ($Only.Trim()) {
    $requested = @($Only.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $unknown = @($requested | Where-Object { $_ -notin $series })
    if ($unknown.Count) { throw "Unknown series: $($unknown -join ', ')" }
    $series = $requested
}

Push-Location $root
try {
    foreach ($name in $series) {
        Write-Host "`n=== GLM $name ==="
        & $Python -X utf8 scripts/_ocr_xuanbian_glm.py --series $name --vols 1 --workers $Workers --scale 0
        if ($LASTEXITCODE -ne 0) { throw "GLM failed for $name (exit $LASTEXITCODE)" }

        if (-not $SkipFallback) {
            Write-Host "`n=== OCR fallback $name ==="
            & $Python -X utf8 scripts/_ocr_xuanbian_glm_fallback.py --series $name --vols 1 --threads $FallbackThreads
            if ($LASTEXITCODE -ne 0) { throw "OCR fallback failed for $name (exit $LASTEXITCODE)" }
        }
    }
} finally {
    Pop-Location
}
