param(
    [ValidateSet('Debug','Release')][string]$Configuration = 'Release',
    [switch]$SelfTest,
    [switch]$Launch,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
trap {
    if ($Launch) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($_.Exception.Message, 'Codex Control Center') | Out-Null
    }
    throw
}
$repoRoot = Split-Path -Parent $PSScriptRoot
$managerPath = Join-Path $repoRoot 'artifacts\manager'
$pointerPath = Join-Path $managerPath 'current.json'
$releasesPath = Join-Path $managerPath 'releases'
if ($SkipBuild -and (Test-Path -LiteralPath $pointerPath)) {
    $currentBuild = Get-Content -LiteralPath $pointerPath -Raw | ConvertFrom-Json
    $resolvedShell = [System.IO.Path]::GetFullPath($currentBuild.shell)
    $approvedPrefix = [System.IO.Path]::GetFullPath($releasesPath) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedShell.StartsWith($approvedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $resolvedShell)) {
        throw 'The manager release pointer is invalid. Rebuild without -SkipBuild.'
    }
    if ($Launch) {
        Start-Process -FilePath $resolvedShell -ArgumentList @('--root', ('"' + $repoRoot + '"')) -WindowStyle Normal | Out-Null
    }
    return
}
$releaseId = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
$outputPath = Join-Path $releasesPath $releaseId
$dotnetPath = (Get-Command dotnet.exe -ErrorAction SilentlyContinue).Source
if (-not $dotnetPath) {
    $dotnetPath = Join-Path $env:ProgramFiles 'dotnet\dotnet.exe'
}
if (-not (Test-Path -LiteralPath $dotnetPath)) {
    throw '.NET 10 SDK is required. Install it, then run this file again.'
}
$cargoPath = Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe'
if (-not (Test-Path -LiteralPath $cargoPath)) { throw 'Rust stable toolchain is required.' }
$manifest = Join-Path $repoRoot 'manager\service\Cargo.toml'
& $cargoPath build --locked --release --manifest-path $manifest
if ($LASTEXITCODE -ne 0) { throw 'Rust manager service build failed.' }
$projects = @(
    'manager\RuntimeProxy\Codex.ControlCenter.RuntimeProxy.csproj',
    'manager\Shell\Codex.ControlCenter.Shell.csproj'
)
foreach ($project in $projects) {
    & $dotnetPath publish (Join-Path $repoRoot $project) -c $Configuration -r win-x64 --self-contained false -o $outputPath --nologo
    if ($LASTEXITCODE -ne 0) { throw "Build failed: $project" }
}
$sshProject = Join-Path $repoRoot 'manager\SshProxy\Codex.ControlCenter.SshProxy.csproj'
if (Test-Path -LiteralPath $sshProject) {
    & $dotnetPath publish $sshProject -c $Configuration -r win-x64 --self-contained false -o (Join-Path $outputPath 'ssh') --nologo
    if ($LASTEXITCODE -ne 0) { throw 'Build failed: SSH bootstrap' }
}
Copy-Item -LiteralPath (Join-Path $repoRoot 'manager\service\target\release\codex-workspace-service.exe') -Destination $outputPath
$compatibility = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList '--write-shell-compatibility' -WindowStyle Hidden -PassThru
if (-not $compatibility.WaitForExit(10000) -or $compatibility.ExitCode -ne 0) {
    throw 'Reading compiled shell compatibility failed; current release is unchanged.'
}
Write-Output "Manager built: $outputPath"
$bundlePython = (Get-Command python.exe -ErrorAction Stop).Source
& $bundlePython -X utf8 (Join-Path $repoRoot 'scripts\prepare_manager_desktop.py') $repoRoot
if ($LASTEXITCODE -ne 0) { throw 'Preparing isolated desktop assets failed; current release is unchanged.' }
& $bundlePython -X utf8 (Join-Path $repoRoot 'scripts\stage_manager_bundle.py') $repoRoot $outputPath
if ($LASTEXITCODE -ne 0) { throw 'Freezing manager runtime bundle failed; current release is unchanged.' }
$currentBuild = [ordered]@{
    version = 1
    native_viewport_version = 1
    created_at = [DateTime]::UtcNow.ToString('o')
    directory = $outputPath
    shell = Join-Path $outputPath 'Codex.ControlCenter.exe'
    supervisor = Join-Path $outputPath 'codex-workspace-service.exe'
    runtime_proxy = Join-Path $outputPath 'Codex.ControlCenter.RuntimeProxy.exe'
    ssh_proxy = if (Test-Path -LiteralPath (Join-Path $outputPath 'ssh\ssh.exe')) { Join-Path $outputPath 'ssh\ssh.exe' } else { $null }
}
if ($SelfTest) {
    $reportPath = Join-Path $repoRoot 'work\control-center-native-host-test.json'
    $arguments = '--native-host-self-test --report "' + $reportPath + '"'
    $test = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if (-not $test.WaitForExit(30000)) { throw "Fixture self-test did not finish in 30 seconds. PID $($test.Id), report $reportPath. No process was terminated." }
    if ($test.ExitCode -ne 0) { throw "Native host self-test failed. Review $reportPath" }
    Get-Content -LiteralPath $reportPath
    $captionReport = Join-Path $repoRoot 'work\control-center-window-controls-test.json'
    $captionArguments = '--window-controls-self-test --root "' + $repoRoot + '" --report "' + $captionReport + '"'
    $captionTest = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList $captionArguments -WindowStyle Hidden -PassThru
    if (-not $captionTest.WaitForExit(15000)) { throw "Window control test exceeded 15 seconds. PID $($captionTest.Id). Current release is unchanged." }
    if ($captionTest.ExitCode -ne 0) { throw "Window control test failed. Review $captionReport" }
    Get-Content -LiteralPath $captionReport
    $latencyReport = Join-Path $repoRoot 'work\control-center-responsiveness-test.json'
    $latencyTest = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList ('--responsiveness-self-test --report "' + $latencyReport + '"') -WindowStyle Hidden -PassThru
    if (-not $latencyTest.WaitForExit(10000)) { throw 'Responsiveness self-test timed out. Current release is unchanged.' }
    if ($latencyTest.ExitCode -ne 0) { throw "Responsiveness self-test failed. Review $latencyReport" }
    Get-Content -LiteralPath $latencyReport
    $notificationReport = Join-Path $repoRoot 'work\control-center-notification-test.json'
    $notificationTest = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList ('--notification-self-test --report "' + $notificationReport + '"') -WindowStyle Hidden -PassThru
    if (-not $notificationTest.WaitForExit(15000)) { throw 'Notification self-test timed out. Current release is unchanged.' }
    if ($notificationTest.ExitCode -ne 0) { throw "Notification self-test failed. Review $notificationReport" }
    Get-Content -LiteralPath $notificationReport
    $notesReport = Join-Path $repoRoot 'work\control-center-notes-test.json'
    $notesTest = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList ('--notes-self-test --report "' + $notesReport + '"') -WindowStyle Hidden -PassThru
    if (-not $notesTest.WaitForExit(45000)) { throw 'Notes/checklist self-test timed out. Current release is unchanged.' }
    if ($notesTest.ExitCode -ne 0) { throw "Notes/checklist self-test failed. Review $notesReport" }
    Get-Content -LiteralPath $notesReport
    $layoutReport = Join-Path $repoRoot 'work\control-center-layout-test.json'
    $layoutTest = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList ('--layout-self-test --root "' + $repoRoot + '" --report "' + $layoutReport + '"') -WindowStyle Hidden -PassThru
    if (-not $layoutTest.WaitForExit(20000)) { throw 'Workspace layout self-test timed out. Current release is unchanged.' }
    if ($layoutTest.ExitCode -ne 0) { throw "Workspace layout self-test failed. Review $layoutReport" }
    Get-Content -LiteralPath $layoutReport
    $orderReport = Join-Path $repoRoot 'work\control-center-profile-order-test.json'
    $orderTest = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList ('--profile-order-self-test --report "' + $orderReport + '"') -WindowStyle Hidden -PassThru
    if (-not $orderTest.WaitForExit(15000)) { throw 'Profile order self-test timed out. Current release is unchanged.' }
    if ($orderTest.ExitCode -ne 0) { throw "Profile order self-test failed. Review $orderReport" }
    Get-Content -LiteralPath $orderReport
    $skillsReport = Join-Path $repoRoot 'work\control-center-personal-skills-test.json'
    $skillsTest = Start-Process -FilePath (Join-Path $outputPath 'Codex.ControlCenter.exe') -ArgumentList ('--personal-skills-self-test --report "' + $skillsReport + '"') -WindowStyle Hidden -PassThru
    if (-not $skillsTest.WaitForExit(15000)) { throw 'Personal skills self-test timed out. Current release is unchanged.' }
    if ($skillsTest.ExitCode -ne 0) { throw "Personal skills self-test failed. Review $skillsReport" }
    Get-Content -LiteralPath $skillsReport
}
$temporaryPointer = Join-Path $managerPath ('current.' + [guid]::NewGuid().ToString('N') + '.tmp')
[System.IO.File]::WriteAllText($temporaryPointer, ($currentBuild | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
Move-Item -LiteralPath $temporaryPointer -Destination $pointerPath -Force
if ($Launch) {
    Start-Process -FilePath $currentBuild.shell -ArgumentList @('--root', ('"' + $repoRoot + '"')) -WindowStyle Normal | Out-Null
}
