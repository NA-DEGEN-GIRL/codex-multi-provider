# Starts an already-built release. Building/deploying belongs to build-manager.ps1.
# The shell requests UAC and verifies the same Windows user before opening data.
$ErrorActionPreference = 'Stop'
try {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $releaseRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot 'artifacts\manager\releases')) + [IO.Path]::DirectorySeparatorChar
    $pointer = Get-Content -LiteralPath (Join-Path $repoRoot 'artifacts\manager\current.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $executable = [IO.Path]::GetFullPath([string]$pointer.shell)
    if (-not $executable.StartsWith($releaseRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($executable) -ne 'Codex.ControlCenter.exe' -or
        -not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw 'The manager release pointer is invalid. Build the manager first.'
    }
    $compatibility = Get-Content -LiteralPath (Join-Path (Split-Path -Parent $executable) 'shell-compatibility.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$compatibility.revision -lt 85) {
        throw 'Administrator mode requires workspace revision 85 or newer. Build and apply it after finishing your work.'
    }
    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $arguments = '--root "' + $repoRoot + '" --require-administrator --expected-user-sid "' + $userSid + '"'
    Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Normal | Out-Null
}
catch {
    Add-Type -AssemblyName PresentationFramework
    [Windows.MessageBox]::Show($_.Exception.Message, 'Codex workspace - administrator mode') | Out-Null
    exit 1
}
