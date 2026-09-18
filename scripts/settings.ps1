$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Security
[Windows.Forms.Application]::EnableVisualStyles()
$root = Split-Path -Parent $PSScriptRoot
$profileDir = Join-Path $root 'profiles'
$settingsPath = Join-Path $profileDir 'provider.json'
$secretPath = Join-Path $profileDir 'deepseek.dpapi'
New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
$form = New-Object Windows.Forms.Form
$form.Text = 'Codex 실험실 - DeepSeek 설정'
$form.Size = New-Object Drawing.Size(620, 360)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.Font = New-Object Drawing.Font('맑은 고딕', 10)
$labels = @('Responses API 주소', '실제 API 모델 ID', 'API 키 (새로 입력할 때만)')
$boxes = @()
for ($i = 0; $i -lt 3; $i++) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $labels[$i]
    $label.Location = New-Object Drawing.Point(20, (24 + $i * 62))
    $label.AutoSize = $true
    $form.Controls.Add($label)
    $box = New-Object Windows.Forms.TextBox
    $box.Location = New-Object Drawing.Point(220, (20 + $i * 62))
    $box.Size = New-Object Drawing.Size(355, 28)
    $form.Controls.Add($box)
    $boxes += $box
}
$boxes[0].Text = 'https://api.deepseek.com'
$boxes[1].Text = 'deepseek-flash'
$boxes[2].UseSystemPasswordChar = $true
if (Test-Path -LiteralPath $settingsPath) {
    $saved = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    $boxes[0].Text = $saved.base_url
    $boxes[1].Text = $saved.model
}
$note = New-Object Windows.Forms.Label
$note.Location = New-Object Drawing.Point(20, 210)
$note.Size = New-Object Drawing.Size(555, 45)
$note.Text = '키는 Windows 사용자 암호화로 이 실험 폴더에 저장합니다. 저장만으로 API가 호출되지는 않습니다.'
if (Test-Path -LiteralPath $secretPath) { $note.Text += ' 현재 키: 등록됨.' }
$form.Controls.Add($note)
$save = New-Object Windows.Forms.Button
$save.Text = '저장'
$save.Location = New-Object Drawing.Point(365, 268)
$save.Size = New-Object Drawing.Size(100, 34)
$save.Add_Click({
    try {
        $uri = [Uri]$boxes[0].Text.Trim()
        if ($uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw '인증 정보나 쿼리가 없는 HTTPS API 주소를 입력하세요.' }
        $model = $boxes[1].Text.Trim()
        if ($model -notmatch '^[a-zA-Z0-9._:/-]+$') { throw '올바른 모델 ID를 입력하세요.' }
        if ($boxes[2].Text.Length -gt 0) {
            $plain = [Text.Encoding]::UTF8.GetBytes($boxes[2].Text.Trim())
            try { $protected = [Security.Cryptography.ProtectedData]::Protect($plain, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser) }
            finally { [Array]::Clear($plain, 0, $plain.Length) }
            [IO.File]::WriteAllBytes($secretPath, $protected)
            $boxes[2].Clear()
        }
        $json = @{ base_url = $uri.AbsoluteUri.TrimEnd('/'); model = $model; protocol = 'responses' } | ConvertTo-Json
        [IO.File]::WriteAllText($settingsPath, $json, (New-Object Text.UTF8Encoding($false)))
        $form.DialogResult = 'OK'
        $form.Close()
    } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message, '설정 확인') | Out-Null }
})
$form.Controls.Add($save)
$cancel = New-Object Windows.Forms.Button
$cancel.Text = '닫기'
$cancel.Location = New-Object Drawing.Point(475, 268)
$cancel.Size = New-Object Drawing.Size(100, 34)
$cancel.Add_Click({ $form.Close() })
$form.Controls.Add($cancel)
$form.AcceptButton = $save
$form.CancelButton = $cancel
[void]$form.ShowDialog()
