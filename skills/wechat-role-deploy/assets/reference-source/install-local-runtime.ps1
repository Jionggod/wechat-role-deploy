$ErrorActionPreference = 'Stop'
$workerName = 'WeChatAI-Role-LocalRuntime'
$workerPython = 'C:\Users\YOUR_USER\.codex\skills\mobile-harness\.venv\Scripts\pythonw.exe'
$workerScript = Join-Path $PSScriptRoot 'local_runtime.py'
if (-not (Test-Path -LiteralPath $workerPython) -or -not (Test-Path -LiteralPath $workerScript)) { throw '本地运行程序不存在。' }
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'runtime\local-runtime-stop.json')) { throw '本地模拟已停止，需要明确恢复后再启动。' }
$startupPath = Join-Path ([Environment]::GetFolderPath('Startup')) ($workerName + '.lnk')
$shellLink = New-Object -ComObject WScript.Shell
$shortcut = $shellLink.CreateShortcut($startupPath)
if ((Test-Path -LiteralPath $startupPath) -and ($shortcut.TargetPath -ne $workerPython -or $shortcut.Arguments -notlike ('*' + $workerScript + '*'))) { throw '同名启动入口属于其他程序，不覆盖。' }
$shortcut.TargetPath = $workerPython
$shortcut.Arguments = '-X utf8 "' + $workerScript + '" serve'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.Description = '本地生活模拟时钟；不定时唤醒模型。'
$shortcut.Save()
$worker = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like ('*' + $workerScript + '*serve*')
})
if ($worker.Count -eq 0) {
    Start-Process -FilePath $workerPython -ArgumentList @('-X', 'utf8', ('"' + $workerScript + '"'), 'serve') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
}
[pscustomobject]@{ startup = $startupPath; mode = 'current-user-hidden-worker'; existingProcesses = $worker.Count } | ConvertTo-Json
