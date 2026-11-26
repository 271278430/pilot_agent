$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskState = Join-Path $taskRoot '.local\local-launch.json'
$taskLogRoot = Join-Path $taskRoot '.local\logs'
New-Item -ItemType Directory -Path $taskLogRoot -Force | Out-Null
$env:PYTHONPATH = Join-Path $taskRoot 'src'
$env:PILOT_HOST_ROOT = $taskRoot
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TEMP = Join-Path $taskRoot '.local\tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $taskRoot '.cache\pip'
$env:npm_config_cache = Join-Path $taskRoot '.cache\npm'
if (Test-Path -LiteralPath $taskState) {
    $taskSaved = Get-Content -LiteralPath $taskState -Raw | ConvertFrom-Json
    $taskRunning = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskSaved.host_pid)" -ErrorAction SilentlyContinue
    if ($taskRunning -and $taskRunning.CommandLine.Contains('pilot_host.cli') -and $taskRunning.CommandLine.Contains($taskRoot)) {
        Write-Output 'Pilot 已在运行：http://127.0.0.1:18900'
        exit 0
    }
}
foreach ($taskPort in @(18888,18900)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $taskPort -ErrorAction SilentlyContinue) {
        throw "端口 $taskPort 已被占用，请先核对现有服务。"
    }
}
$taskHost = Start-Process -FilePath $taskPython -ArgumentList @('-B','-m','pilot_host.cli','--root',$taskRoot,'--port','18888') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLogRoot 'local-host.log') -RedirectStandardError (Join-Path $taskLogRoot 'local-host.err')
$taskEntry = Start-Process -FilePath $taskPython -ArgumentList @('-B',(Join-Path $taskRoot 'scripts\local_entry.py'),'--root',$taskRoot,'--port','18900') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLogRoot 'local-entry.log') -RedirectStandardError (Join-Path $taskLogRoot 'local-entry.err')
$taskLaunch = @{ root=$taskRoot; host_pid=$taskHost.Id; entry_pid=$taskEntry.Id; host_port=18888; entry_port=18900; started_at=(Get-Date).ToString('o') } | ConvertTo-Json
[System.IO.File]::WriteAllText($taskState, $taskLaunch, [System.Text.UTF8Encoding]::new($false))
Write-Output 'Pilot 启动中：http://127.0.0.1:18900'
