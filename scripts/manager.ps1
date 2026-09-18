param([string]$RenderPreview, [ValidateSet('baseline','custom','stream')][string]$SmokeScenario)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'LiveProcessOutput.cs')
[Windows.Forms.Application]::EnableVisualStyles()
$root = Split-Path -Parent $PSScriptRoot
function Get-LatestTaskReport {
    $fallback = $null
    foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $root 'artifacts/results') -Filter 'report.json' -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
        try {
            $report = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            if (-not $fallback) { $fallback = $report }
            if ($report.scenario -eq 'custom') { return $report }
        } catch { }
    }
    return $fallback
}
$script:testProcess = $null
$script:liveOutput = $null
$script:runClock = $null
$script:lastOutputAt = $null
$script:streamObserved = $false
$script:appLaunchProcess = $null
$script:appLaunchOutput = $null
$script:smokeExitCode = 1
$form = New-Object Windows.Forms.Form
$form.Text = 'Codex 다중 모델 실험실'
$form.Size = New-Object Drawing.Size(850, 800)
$form.MinimumSize = New-Object Drawing.Size(850, 720)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('맑은 고딕', 10)
$title = New-Object Windows.Forms.Label
$title.Text = '아래에서 모델을 테스트하거나, 별도 실험용 Codex 앱을 열 수 있습니다.'
$title.Location = New-Object Drawing.Point(20, 18)
$title.Size = New-Object Drawing.Size(790, 30)
$form.Controls.Add($title)
$mode = New-Object Windows.Forms.ComboBox
$mode.DropDownStyle = 'DropDownList'
[void]$mode.Items.Add('기본 런타임 (무수정 0.153.4)')
[void]$mode.Items.Add('확장 런타임 (외부 자식 허용)')
$mode.SelectedIndex = 1
$mode.Location = New-Object Drawing.Point(20, 60)
$mode.Size = New-Object Drawing.Size(340, 30)
$form.Controls.Add($mode)
$status = New-Object Windows.Forms.Label
$status.Location = New-Object Drawing.Point(380, 64)
$status.Size = New-Object Drawing.Size(425, 28)
$form.Controls.Add($status)
$settings = New-Object Windows.Forms.Button
$settings.Text = 'DeepSeek API 설정'
$settings.Location = New-Object Drawing.Point(20, 108)
$settings.Size = New-Object Drawing.Size(175, 36)
$settings.Add_Click({
    if ($script:testProcess) { return }
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = (Get-Command pwsh.exe).Source
    $psi.Arguments = '-NoProfile -STA -File "' + (Join-Path $PSScriptRoot 'settings.ps1') + '"'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    [void][Diagnostics.Process]::Start($psi)
})
$form.Controls.Add($settings)
$note = New-Object Windows.Forms.Label
$note.Text = '테스트 버튼은 실제 모델 사용량을 소비합니다. 파일 작업은 실험용 폴더에 한정됩니다.'
$note.Location = New-Object Drawing.Point(210, 113)
$note.Size = New-Object Drawing.Size(585, 40)
$form.Controls.Add($note)
$log = New-Object Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = 'Vertical'
$log.WordWrap = $true
$log.Location = New-Object Drawing.Point(20, 398)
$log.Size = New-Object Drawing.Size(790, 320)
$log.Anchor = 'Top,Bottom,Left,Right'
$log.Font = New-Object Drawing.Font('Consolas', 10)
$log.Text = '모드를 선택한 후 테스트를 실행하세요. 기존 앱을 종료할 필요가 없습니다.'
$previous = Get-LatestTaskReport
if ($previous) {
    try {
        $log.Text = ("최근 작업: $($previous.run) / $($previous.status)`r`n작업 폴더: $($previous.workspace)`r`n`r`n" + ($previous.messages | Select-Object -Last 1)) -replace "(?<!`r)`n", "`r`n"
        if ($previous.error) { $log.AppendText("`r`n오류: $($previous.error)") }
    } catch { }
}
$form.Controls.Add($log)
$progress = New-Object Windows.Forms.Label
$progress.Text = '대기 중 · 실행하면 메시지와 도구 진행 상황이 표시됩니다.'
$progress.Location = New-Object Drawing.Point(220, 350)
$progress.Size = New-Object Drawing.Size(590, 42)
$form.Controls.Add($progress)
$script:testButtons = @()
$taskLabel = New-Object Windows.Forms.Label
$taskLabel.Text = '자유 작업 — 매번 새 실험 폴더에서 실행하며, 자식 모델은 Astra가 선택합니다.'
$taskLabel.Location = New-Object Drawing.Point(20, 218)
$taskLabel.Size = New-Object Drawing.Size(790, 28)
$form.Controls.Add($taskLabel)
$taskInput = New-Object Windows.Forms.TextBox
$taskInput.Multiline = $true
$taskInput.ScrollBars = 'Vertical'
$taskInput.Location = New-Object Drawing.Point(20, 250)
$taskInput.Size = New-Object Drawing.Size(790, 85)
$taskInput.Text = '간단한 할 일 목록 웹페이지를 HTML 파일 하나로 만들어줘. 구현과 검토를 자식에게 나눠 맡기고, 비용과 난이도에 따라 DeepSeek 또는 GPT를 선택해. 실제 선택한 모델과 생성한 파일을 보고해.'
$savedPrompt = Join-Path $root 'work/custom-task.txt'
if (Test-Path -LiteralPath $savedPrompt) { $taskInput.Text = [IO.File]::ReadAllText($savedPrompt) }
if ($SmokeScenario -eq 'custom') {
    $taskInput.Text = 'Choose a native child model according to cost and task complexity. Delegate creation of arithmetic.py with add(a, b), and test_arithmetic.py using unittest to verify positive, negative and zero inputs. Have the child run python -m unittest -v and report the results. Do not implement the files yourself. Keep all files in this workspace. Wait for the child and report its actual model.'
}
$form.Controls.Add($taskInput)
$buttonSpecs = @(@('GPT 기본 확인', 'baseline'), @('DeepSeek 도구 확인', 'deepseek'), @('GPT + DeepSeek 위임', 'mixed'), @('Astra 자율 선택', 'selection'), @('자유 작업 실행', 'custom'))
for ($i = 0; $i -lt $buttonSpecs.Count; $i++) {
    $button = New-Object Windows.Forms.Button
    $button.Text = $buttonSpecs[$i][0]
    $button.Tag = $buttonSpecs[$i][1]
    $button.Location = New-Object Drawing.Point((20 + $i * 200), 166)
    if ($button.Tag -eq 'custom') { $button.Location = New-Object Drawing.Point(20, 347) }
    $button.Size = New-Object Drawing.Size(190, 36)
    $button.Add_Click({
        param($sender, $eventArgs)
        if ($script:testProcess) { return }
        $selectedMode = if ($mode.SelectedIndex -eq 0) { 'upstream' } else { 'runtime' }
        $scenario = [string]$sender.Tag
        if ($SmokeScenario -eq 'stream') { $scenario = 'stream' }
        $binary = Join-Path $root "artifacts/$selectedMode/codex.exe"
        if (-not (Test-Path -LiteralPath $binary)) { [void][Windows.Forms.MessageBox]::Show('아직 해당 런타임 빌드가 완료되지 않았습니다.'); return }
        if ($scenario -notin @('baseline','stream') -and -not (Test-Path -LiteralPath (Join-Path $root 'profiles/deepseek.dpapi'))) { [void][Windows.Forms.MessageBox]::Show('DeepSeek API 키를 먼저 설정하세요.'); return }
        if ($selectedMode -eq 'upstream' -and $scenario -in @('mixed','selection')) { [void][Windows.Forms.MessageBox]::Show('외부 자식 위임은 확장 런타임에서 테스트하세요.'); return }
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = (Get-Command python.exe).Source
        $psi.Arguments = '"' + (Join-Path $PSScriptRoot 'live_test.py') + '" --mode ' + $selectedMode + ' --scenario ' + $scenario
        if ($scenario -eq 'stream') { $psi.Arguments = '"' + (Join-Path $root 'tests/stream_fixture.py') + '"' }
        if ($scenario -eq 'custom') {
            if ([string]::IsNullOrWhiteSpace($taskInput.Text)) { return }
            $promptFile = Join-Path $root 'work/custom-task.txt'
            [IO.File]::WriteAllText($promptFile, $taskInput.Text, [Text.UTF8Encoding]::new($false))
            $psi.Arguments += ' --prompt-file "' + $promptFile + '"'
        }
        $psi.WorkingDirectory = $root
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
        $script:testProcess = [Diagnostics.Process]::Start($psi)
        $script:liveOutput = [LabLiveProcessOutput]::new($script:testProcess.StandardOutput, $script:testProcess.StandardError)
        $script:runClock = [Diagnostics.Stopwatch]::StartNew()
        $script:lastOutputAt = [DateTime]::Now
        $log.Text = "실행 중: $selectedMode / $scenario`r`n메시지와 도구 실행 결과를 실시간으로 표시합니다.`r`n`r`n"
        $progress.Text = '실행 중 · 00:00 · 런타임 시작 중'
        $mode.Enabled = $false
        $settings.Enabled = $false
        $taskInput.Enabled = $false
        foreach ($item in $script:testButtons) { $item.Enabled = $false }
    })
    $script:testButtons += $button
    $form.Controls.Add($button)
}
$folder = New-Object Windows.Forms.Button
$folder.Text = '테스트 결과 폴더'
$folder.Location = New-Object Drawing.Point(20, 728)
$folder.Size = New-Object Drawing.Size(180, 32)
$folder.Anchor = 'Bottom,Left'
$folder.Add_Click({
    $path = Join-Path $root 'artifacts/results'
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    Start-Process explorer.exe -ArgumentList ('"' + $path + '"')
})
$form.Controls.Add($folder)
$files = New-Object Windows.Forms.Button
$files.Text = '최근 생성 파일 폴더'
$files.Location = New-Object Drawing.Point(220, 728)
$files.Size = New-Object Drawing.Size(190, 32)
$files.Anchor = 'Bottom,Left'
$files.Add_Click({
    $report = Get-LatestTaskReport
    if ($report) {
        if ($report.workspace -and (Test-Path -LiteralPath $report.workspace -PathType Container)) {
            Start-Process explorer.exe -ArgumentList ('"' + $report.workspace + '"')
        }
    }
})
$form.Controls.Add($files)
$script:appButtons = @()
foreach ($appSpec in @(@('원래 Codex 앱 열기', 'original'), @('실험용 Codex 앱 열기', 'runtime'))) {
    $appButton = New-Object Windows.Forms.Button
    $appButton.Text = $appSpec[0]
    $appButton.Tag = $appSpec[1]
    $appButton.Location = New-Object Drawing.Point((420 + $script:appButtons.Count * 200), 728)
    $appButton.Size = New-Object Drawing.Size(190, 32)
    $appButton.Anchor = 'Bottom,Left'
    $appButton.Add_Click({
        param($sender, $eventArgs)
        if ($script:appLaunchProcess) { return }
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = (Get-Command python.exe).Source
        $psi.Arguments = '"' + (Join-Path $PSScriptRoot 'desktop_launch.py') + '" --mode ' + $sender.Tag
        $psi.WorkingDirectory = $root
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
        $script:appLaunchProcess = [Diagnostics.Process]::Start($psi)
        $script:appLaunchOutput = [LabLiveProcessOutput]::new($script:appLaunchProcess.StandardOutput, $script:appLaunchProcess.StandardError)
        $log.AppendText("`r`n[$($sender.Text)] 실행 준비 중…`r`n")
        foreach ($item in $script:appButtons) { $item.Enabled = $false }
    })
    $script:appButtons += $appButton
    $form.Controls.Add($appButton)
}
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 200
$timer.Add_Tick({
    $keyState = if (Test-Path -LiteralPath (Join-Path $root 'profiles/deepseek.dpapi')) { '키 등록됨' } else { '키 미등록' }
    $runtimeState = if (Test-Path -LiteralPath (Join-Path $root 'artifacts/runtime/codex.exe')) { '확장 빌드 준비됨' } else { '확장 빌드 대기' }
    $status.Text = "$runtimeState / $keyState"
    if ($script:appLaunchProcess) {
        foreach ($chunk in $script:appLaunchOutput.Drain()) { $log.AppendText(($chunk -replace "(?<!`r)`n", "`r`n")) }
        if ($script:appLaunchProcess.HasExited -and $script:appLaunchOutput.IsCompleted) {
            foreach ($chunk in $script:appLaunchOutput.Drain()) { $log.AppendText(($chunk -replace "(?<!`r)`n", "`r`n")) }
            if ($script:appLaunchProcess.ExitCode -ne 0) { $log.AppendText("앱 실행 준비에 실패했습니다. 위의 오류를 확인하세요.`r`n") }
            $script:appLaunchProcess.Dispose()
            $script:appLaunchProcess = $null
            foreach ($item in $script:appButtons) { $item.Enabled = $true }
        }
    }
    if ($script:testProcess) {
        foreach ($chunk in $script:liveOutput.Drain()) {
            $log.AppendText(($chunk -replace "(?<!`r)`n", "`r`n"))
            $script:lastOutputAt = [DateTime]::Now
        }
        $elapsed = $script:runClock.Elapsed
        $quietSeconds = [int]([DateTime]::Now - $script:lastOutputAt).TotalSeconds
        $progress.Text = ('실행 중 · {0:00}:{1:00} 경과 · 마지막 출력 {2}초 전' -f [int][Math]::Floor($elapsed.TotalMinutes), $elapsed.Seconds, $quietSeconds)
        if ($SmokeScenario -eq 'stream' -and -not $script:streamObserved -and $log.Text.Contains('STREAM_OUTPUT_FIRST') -and -not $script:testProcess.HasExited) {
            $script:streamObserved = $true
            [IO.File]::WriteAllText((Join-Path $root 'work/manager-stream-observed.json'), (@{ observed_before_exit = $true; elapsed_ms = $elapsed.TotalMilliseconds; child_alive = $true } | ConvertTo-Json))
            $bitmap = New-Object Drawing.Bitmap($form.Width, $form.Height)
            try {
                $form.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
                $bitmap.Save((Join-Path $root 'work/manager-stream-running.png'), [Drawing.Imaging.ImageFormat]::Png)
            } finally { $bitmap.Dispose() }
        }
    }
    if ($script:testProcess -and $script:testProcess.HasExited -and $script:liveOutput.IsCompleted) {
        # Drain again after observing reader completion, so the final chunk cannot race exit.
        foreach ($chunk in $script:liveOutput.Drain()) { $log.AppendText(($chunk -replace "(?<!`r)`n", "`r`n")) }
        $script:smokeExitCode = $script:testProcess.ExitCode
        $script:runClock.Stop()
        $log.AppendText("`r`n종료 코드: " + $script:testProcess.ExitCode + "`r`n")
        $outcome = if ($script:testProcess.ExitCode -eq 0) { '완료' } else { '실패 · 아래 오류 확인' }
        $progress.Text = "$outcome · 총 $([int]$script:runClock.Elapsed.TotalSeconds)초"
        if ($SmokeScenario -eq 'stream' -and (-not $script:streamObserved -or -not $log.Text.Contains('STREAM_STDERR_FIRST') -or -not $log.Text.Contains('STREAM_OUTPUT_LAST') -or -not $log.Text.Contains('한글'))) { $script:smokeExitCode = 1 }
        $script:testProcess.Dispose()
        $script:testProcess = $null
        $mode.Enabled = $true
        $settings.Enabled = $true
        $taskInput.Enabled = $true
        foreach ($item in $script:testButtons) { $item.Enabled = $true }
        if ($SmokeScenario) {
            [IO.File]::WriteAllText((Join-Path $root "work/manager-smoke-$SmokeScenario.log"), $log.Text, [Text.UTF8Encoding]::new($false))
            $form.Close()
        }
    }
})
$form.Add_FormClosing({ param($sender,$eventArgs) if ($script:testProcess) { $eventArgs.Cancel = $true; [void][Windows.Forms.MessageBox]::Show('실행 중인 테스트가 끝난 후 닫아주세요. 각 테스트에는 시간 제한이 있습니다.') } })
if ($RenderPreview) {
    $form.Show()
    [Windows.Forms.Application]::DoEvents()
    $bitmap = New-Object Drawing.Bitmap($form.Width, $form.Height)
    try {
        $form.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
        $bitmap.Save($RenderPreview, [Drawing.Imaging.ImageFormat]::Png)
    } finally { $bitmap.Dispose(); $form.Close(); $form.Dispose(); $timer.Dispose() }
    exit 0
}
$timer.Start()
if ($SmokeScenario) {
    $form.Add_Shown({
        $targetScenario = if ($SmokeScenario -eq 'stream') { 'baseline' } else { $SmokeScenario }
        ($script:testButtons | Where-Object Tag -eq $targetScenario).PerformClick()
    })
}
try { [void]$form.ShowDialog() } finally { $timer.Stop(); $timer.Dispose() }
if ($SmokeScenario) { exit $script:smokeExitCode }
