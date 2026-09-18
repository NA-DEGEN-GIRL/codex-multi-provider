[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$SelfTest)

$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$ownedDirectories = @(
    'artifacts\managed-desktop', 'artifacts\original-sync-desktop',
    'artifacts\manager-runtime', 'artifacts\manager\releases',
    'artifacts\desktop', 'artifacts\experimental-desktop'
) | ForEach-Object { [IO.Path]::Combine($taskRoot, $_).TrimEnd('\') + '\' }
$codexNames = @(
    'codex.exe', 'codex-app-server.exe', 'codex-code-mode-host.exe',
    'codex-command-runner.exe', 'codex-windows-sandbox-setup.exe',
    'codex-computer-use-swift.exe', 'codex-workspace-service.exe',
    'Codex.ControlCenter.exe', 'Codex.ControlCenter.RuntimeProxy.exe'
)

function Test-CodexProcess($Row) {
    if ([int]$Row.ProcessId -eq $PID) { return $false }
    if ($codexNames -contains [string]$Row.Name) { return $true }
    $image = [string]$Row.ExecutablePath
    if ($image -match '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\') { return $true }
    if ($image -match '\\(?:Local|Roaming)\\OpenAI\\Codex\\') { return $true }
    foreach ($directory in $ownedDirectories) {
        if ($image.StartsWith($directory, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    # Include a Python proxy/service orphaned after its Electron parent exited.
    # Read command lines only for selection; never print or log their contents.
    if ($Row.Name -in @('python.exe', 'pythonw.exe', 'node.exe')) {
        $command = [string]$Row.CommandLine
        if ($command.IndexOf($taskRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $command -match '(?i)[\\/]scripts[\\/](?:manager_core[\\/]runtime_proxy|control_center)\.py(?:["\s]|$)') {
            return $true
        }
        if ($command -match '(?i)[\\/]@openai[\\/]codex[\\/]bin[\\/]codex\.js(?:["\s]|$)') { return $true }
    }
    return $false
}

function Get-CodexProcesses {
    # A new snapshot also catches a helper created while its parent was stopping.
    @(Get-CimInstance Win32_Process | Where-Object { Test-CodexProcess $_ })
}

if ($SelfTest) {
    $cases = @(
        @('codex.exe', 'C:\tools\codex.exe', '', $true),
        @('ChatGPT.exe', 'C:\Program Files\WindowsApps\OpenAI.Codex_1_x64__test\app\ChatGPT.exe', '', $true),
        @('ChatGPT.exe', "$taskRoot\artifacts\managed-desktop\test\ChatGPT.exe", '', $true),
        @('ChatGPT.exe', 'C:\Apps\ChatGPT\ChatGPT.exe', '', $false),
        @('node.exe', 'C:\Program Files\nodejs\node.exe', 'node C:\my-site\server.js', $false),
        @('pythonw.exe', 'C:\Python313\pythonw.exe', "pythonw `"$taskRoot\scripts\manager_core\runtime_proxy.py`"", $true),
        @('python.exe', 'C:\Python313\python.exe', "python `"$taskRoot\my-script.py`"", $false),
        @('wslhost.exe', 'C:\Windows\System32\wslhost.exe', '', $false)
    )
    foreach ($case in $cases) {
        $row = [pscustomobject]@{ ProcessId = -1; Name = $case[0]; ExecutablePath = $case[1]; CommandLine = $case[2] }
        if ((Test-CodexProcess $row) -ne $case[3]) { throw "Selection check failed: $($case[0]) / $($case[1])" }
    }
    Write-Host "PASS: $($cases.Count) process selection checks. No processes were stopped."
    exit 0
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CodexStopNative {
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool TerminateProcess(IntPtr process, uint exitCode);
}
'@

$results = [Collections.Generic.List[object]]::new()
for ($pass = 1; $pass -le 3; $pass++) {
    $targets = @(Get-CodexProcesses)
    if ($targets.Count -eq 0) { break }
    Write-Host "Pass ${pass}: $($targets.Count) Codex processes"
    foreach ($row in $targets) {
        $process = $null
        try {
            $process = [Diagnostics.Process]::GetProcessById([int]$row.ProcessId)
            # Hold the kernel handle before checking identity. Never terminate a
            # newly reused PID or use taskkill /IM against unrelated ChatGPT apps.
            [IntPtr]$processHandle = [IntPtr]::Zero
            if ($process.HasExited) { continue }
            $processHandle = $process.SafeHandle.DangerousGetHandle()
            if ($processHandle -eq [IntPtr]::Zero -or $process.HasExited) { continue }
            $created = $process.StartTime.ToUniversalTime()
            if ([Math]::Abs(($created - $row.CreationDate.ToUniversalTime()).TotalMilliseconds) -gt 1) { continue }
            if ($row.ExecutablePath -and -not [string]::Equals($process.MainModule.FileName,
                    [string]$row.ExecutablePath, [StringComparison]::OrdinalIgnoreCase)) { continue }
            if (-not $PSCmdlet.ShouldProcess("$($row.Name) PID $($row.ProcessId)", 'Force-close Codex process')) { continue }
            if (-not [CodexStopNative]::TerminateProcess($processHandle, 1) -and -not $process.HasExited) {
                throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
            }
            $results.Add([pscustomobject]@{ pid = [int]$row.ProcessId; name = $row.Name; status = 'termination_requested' })
            Write-Host "Stopped $($row.Name) (PID $($row.ProcessId))"
        } catch {
            # Process disappearance is expected while other Codex processes exit.
            if (Get-Process -Id $row.ProcessId -ErrorAction SilentlyContinue) {
                $results.Add([pscustomobject]@{ pid = [int]$row.ProcessId; name = $row.Name; status = 'failed'; error = $_.Exception.Message })
                Write-Warning "$($row.Name) PID $($row.ProcessId): $($_.Exception.Message)"
            }
        } finally {
            if ($null -ne $process) { $process.Dispose() }
        }
    }
    if ($WhatIfPreference) { Write-Host 'Preview only. Nothing was stopped.'; exit 0 }
    Start-Sleep -Milliseconds 700
}
$remaining = @(Get-CodexProcesses | Select-Object ProcessId, Name)
$logDirectory = Join-Path $taskRoot 'work\codex-shutdown'
[void][IO.Directory]::CreateDirectory($logDirectory)
$logPath = Join-Path $logDirectory ('stop-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.json')
[pscustomobject]@{ finished = (Get-Date).ToString('o'); results = @($results.ToArray()); remaining = $remaining } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $logPath -Encoding UTF8
Write-Host "Log: $logPath"
if ($remaining.Count) {
    $remaining | Format-Table -AutoSize
    Write-Warning 'Some Codex processes remain. If access was denied, run Stop-All-Codex.cmd as administrator.'
    exit 1
}
Write-Host 'All detected Windows Codex processes have exited. No history or login files were deleted.'
exit 0
