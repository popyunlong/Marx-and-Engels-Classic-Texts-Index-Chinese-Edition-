[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$Server = 'https://mazhuzuojiansuo.com',
    [string]$SourceDirectory = 'D:\CodexData\outputs\journal-monitor',
    [string]$StateDirectory = 'D:\CodexData\data\research-sync',
    [switch]$RunOnce
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = (& py -3.10 -c 'import sys; print(sys.executable)').Trim() }
if (-not (Test-Path -LiteralPath $SourceDirectory -PathType Container)) { throw '国内资料目录不存在' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python 运行环境不存在' }
if (([uri]$Server).Scheme -ne 'https') { throw '推送目标必须使用 HTTPS' }
if (([IO.Path]::GetFullPath($StateDirectory)).StartsWith('C:\',[StringComparison]::OrdinalIgnoreCase)) { throw '同步状态与凭据应保存在 D 盘' }
New-Item -ItemType Directory -Force -Path $StateDirectory | Out-Null
$credentialFile = Join-Path $StateDirectory 'import-token.dpapi'
if (-not (Test-Path -LiteralPath $credentialFile)) {
    Read-Host '粘贴网站后台生成的仅导入凭据' -AsSecureString | ConvertFrom-SecureString | Set-Content -LiteralPath $credentialFile
}
$runner = Join-Path $StateDirectory 'run-sync.ps1'
$quote = { param($s) "'" + $s.Replace("'", "''") + "'" }
$lines = @(
    '$ErrorActionPreference = ''Stop''',
    ('$secure = Get-Content -LiteralPath ' + (& $quote $credentialFile) + ' | ConvertTo-SecureString'),
    '$env:MARX_RESEARCH_IMPORT_TOKEN = [Net.NetworkCredential]::new('''',$secure).Password',
    '$env:PYTHONDONTWRITEBYTECODE = ''1''',
    'try {',
    ('  & ' + (& $quote $Python) + ' ' + (& $quote (Join-Path $repo 'scripts\research_domestic_sync.py')) + ' --root ' + (& $quote $SourceDirectory) + ' --state ' + (& $quote (Join-Path $StateDirectory 'state.json')) + ' --server ' + (& $quote $Server)),
    '  if ($LASTEXITCODE -ne 0) { throw ''同步失败，下一轮重试'' }',
    '} finally { Remove-Item Env:MARX_RESEARCH_IMPORT_TOKEN -ErrorAction SilentlyContinue }'
)
$lines | Set-Content -LiteralPath $runner -Encoding utf8
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $runner + '"')
$timer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$login = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'MarxResearchDomesticSync' -Action $action -Trigger @($timer,$login) -Settings $settings -Description '国内期刊资料仅推送至网站草稿区；不发布、不发信' -Force | Out-Null
if ($RunOnce) { & $runner }
Write-Output '国内研究动态同步已安装：每15分钟检查，登录后补跑。凭据由当前Windows账户加密。'
