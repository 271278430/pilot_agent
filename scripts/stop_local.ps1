$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskState = Join-Path $taskRoot '.local\local-launch.json'
if (-not (Test-Path -LiteralPath $taskState)) { Write-Output '没有本地启动记录。'; exit 0 }
$taskSaved = Get-Content -LiteralPath $taskState -Raw | ConvertFrom-Json
if ($taskSaved.root -ne $taskRoot) { throw '启动记录目录不匹配。' }
foreach ($taskItem in @(@{id=$taskSaved.host_pid; hint='pilot_host.cli'},@{id=$taskSaved.entry_pid; hint='local_entry.py'})) {
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskItem.id)" -ErrorAction SilentlyContinue
    if (-not $taskProcess) { continue }
    if (-not $taskProcess.CommandLine.Contains($taskRoot) -or -not $taskProcess.CommandLine.Contains($taskItem.hint)) { throw '进程身份已改变，请先核对。' }
    $taskSnapshot = @(Get-CimInstance Win32_Process)
    function Get-TaskDescendants([int]$taskParentId) {
        foreach ($taskChild in $taskSnapshot | Where-Object { $_.ParentProcessId -eq $taskParentId }) {
            Get-TaskDescendants $taskChild.ProcessId
            $taskChild
        }
    }
    $taskOwned = @(Get-TaskDescendants $taskProcess.ProcessId) + @($taskProcess)
    foreach ($taskOwnedItem in $taskOwned) {
        $taskCurrent = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskOwnedItem.ProcessId)" -ErrorAction SilentlyContinue
        if ($taskCurrent -and $taskCurrent.CreationDate -eq $taskOwnedItem.CreationDate) {
            try { Stop-Process -Id $taskCurrent.ProcessId -ErrorAction Stop }
            catch {
                $taskStillRunning = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskOwnedItem.ProcessId)" -ErrorAction SilentlyContinue
                if ($taskStillRunning -and $taskStillRunning.CreationDate -eq $taskOwnedItem.CreationDate) { throw }
            }
        }
    }
}
Write-Output '本地 Pilot 已停止，历史和文件已保留。'
