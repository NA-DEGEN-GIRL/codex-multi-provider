# Keep this bootstrap ASCII: Explorer starts it with Windows PowerShell 5.1.
param(
    [ValidateSet('Lab', 'Desktop')][string]$Target = 'Lab',
    [ValidateSet('runtime', 'original')][string]$Mode = 'runtime',
    [switch]$Check,
    [switch]$Wait,
    [ValidateSet('baseline', 'custom', 'stream')][string]$SmokeScenario,
    [string]$RenderPreview
)
$ErrorActionPreference = 'Stop'
$labRoot = Split-Path -Parent $PSScriptRoot

function Find-LabExecutable {
    param([string[]]$Candidates, [string]$Name)
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            # Store execution aliases can open an installer instead of Python.
            if ($Name -like 'python*' -and $candidate -match '\\Microsoft\\WindowsApps\\') { continue }
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw "Cannot locate $Name. Details: $labRoot\work\launcher-error.log"
}

function Find-OnPath {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
}

try {
    $pwshPath = Find-LabExecutable -Name 'pwsh.exe' -Candidates @(
        (Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\PowerShell\7\pwsh.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe'),
        (Find-OnPath 'pwsh.exe')
    )
    $pythonCandidates = @(
        (Join-Path $env:SystemDrive 'Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
    )
    foreach ($registryRoot in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($versionKey in (Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending)) {
            $version = $null
            if (-not [version]::TryParse(($versionKey.PSChildName -replace '-.*$', ''), [ref]$version) -or $version -lt [version]'3.11') { continue }
            $installKey = Get-Item -LiteralPath (Join-Path $versionKey.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($installKey -and $installKey.GetValue('')) { $pythonCandidates += Join-Path $installKey.GetValue('') 'python.exe' }
        }
    }
    $pythonCandidates += Find-OnPath 'python.exe'
    $pythonPath = Find-LabExecutable -Name 'python.exe' -Candidates $pythonCandidates
    $pythonwPath = Find-LabExecutable -Name 'pythonw.exe' -Candidates @((Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'))
    $gitPath = Find-LabExecutable -Name 'git.exe' -Candidates @(
        (Join-Path $env:ProgramFiles 'Git\cmd\git.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe'),
        (Find-OnPath 'git.exe')
    )
    # Only this launcher and its children receive these paths. No user/system PATH writes.
    $dependencyDirs = @((Split-Path -Parent $pwshPath), (Split-Path -Parent $pythonPath), (Split-Path -Parent $gitPath))
    $env:PATH = (($dependencyDirs + @($env:PATH)) -join ';')
    if ($Check) {
        @{ target = $Target; powershell = $pwshPath; python = $pythonPath; pythonw = $pythonwPath; git = $gitPath } | ConvertTo-Json
        exit 0
    }
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.WorkingDirectory = $labRoot
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    if ($Target -eq 'Lab') {
        $psi.FileName = $pwshPath
        $psi.Arguments = '-NoProfile -STA -WindowStyle Hidden -File "' + (Join-Path $PSScriptRoot 'manager.ps1') + '"'
        if ($SmokeScenario) { $psi.Arguments += ' -SmokeScenario ' + $SmokeScenario }
        if ($RenderPreview) { $psi.Arguments += ' -RenderPreview "' + $RenderPreview + '"' }
    } else {
        $psi.FileName = if ($Wait) { $pythonPath } else { $pythonwPath }
        $psi.Arguments = '"' + (Join-Path $PSScriptRoot 'desktop_launch.py') + '" --mode ' + $Mode + ' --notify'
    }
    $process = [Diagnostics.Process]::Start($psi)
    if ($Wait -or $SmokeScenario -or $RenderPreview) {
        $process.WaitForExit()
        $result = $process.ExitCode
        $process.Dispose()
        exit $result
    }
    $process.Dispose()
    exit 0
} catch {
    $message = $_.Exception.Message
    $workPath = Join-Path $labRoot 'work'
    [IO.Directory]::CreateDirectory($workPath) | Out-Null
    [IO.File]::WriteAllText((Join-Path $workPath 'launcher-error.log'), $message)
    if ($Check -or $Wait -or $SmokeScenario -or $RenderPreview) {
        [Console]::Error.WriteLine($message)
    } else {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show($message, 'Codex lab launcher')
    }
    exit 1
}
