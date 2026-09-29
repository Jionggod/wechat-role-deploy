param([switch]$CheckProactive)
$ErrorActionPreference = 'Stop'
$bridgeRoot = $PSScriptRoot
$bridgePython = 'C:\Users\YOUR_USER\.codex\skills\mobile-harness\.venv\Scripts\python.exe'
$bridgeRuntime = Join-Path $bridgeRoot 'runtime'
$bridgeStatusPath = Join-Path $bridgeRuntime 'status.json'
$stopPath = Join-Path $bridgeRuntime 'stop.json'
if (Test-Path -LiteralPath $stopPath) { throw '监听已被停止，不自动撤销停止标志。' }
$bridgeProcess = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -like '*D:\RoleBridge\run.py*run --send*'
} | Select-Object -First 1
if (-not $bridgeProcess) {
    $stamp = Get-Date -Format 'yyyyMMddTHHmmss'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:Path = 'C:\Android\sdk\platform-tools;' + $env:Path
    Start-Process -FilePath $bridgePython -ArgumentList @('-X','utf8',(Join-Path $bridgeRoot 'run.py'),'run','--send') `
        -WorkingDirectory $bridgeRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $bridgeRuntime "$stamp-stdout.log") `
        -RedirectStandardError (Join-Path $bridgeRuntime "$stamp-stderr.log") | Out-Null
    Start-Sleep -Seconds 12
}
if ($CheckProactive) { & $bridgePython -X utf8 (Join-Path $bridgeRoot 'run.py') proactive-check }
if (Test-Path -LiteralPath $bridgeStatusPath) {
    $bridgeStatus = Get-Content -LiteralPath $bridgeStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    [pscustomobject]@{
        status = $bridgeStatus.status
        at = $bridgeStatus.at
        pid = $bridgeStatus.pid
        primary = $bridgeStatus.primary
        fallback = $bridgeStatus.fallback
        pending = $bridgeStatus.pending.id
        awaitingReply = $bridgeStatus.awaiting_reply
        error = $bridgeStatus.error
    } | ConvertTo-Json
} else { throw '监听尚未产生状态文件，请检查本机日志。' }
