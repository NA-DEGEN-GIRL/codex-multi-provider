$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = 'C:\Python313\pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Python 3.13 was not found.' }
$taskScript = Join-Path $PSScriptRoot 'start_synced_original.py'
$taskCurrent = Join-Path $taskRoot 'artifacts\manager\current.json'
if (Test-Path -LiteralPath $taskCurrent) {
    $taskRelease = Get-Content -LiteralPath $taskCurrent -Raw | ConvertFrom-Json
    $taskFrozen = Join-Path $taskRelease.directory 'scripts\start_synced_original.py'
    if (Test-Path -LiteralPath $taskFrozen) { $taskScript = $taskFrozen }
}
$taskInfo = New-Object Diagnostics.ProcessStartInfo
$taskInfo.FileName = $taskPython
$taskInfo.Arguments = '"' + $taskScript + '" --root "' + $taskRoot + '" --notify'
$taskInfo.UseShellExecute = $false
$taskInfo.CreateNoWindow = $true
[void][Diagnostics.Process]::Start($taskInfo)
