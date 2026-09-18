param([ValidateSet('upstream','runtime')][string]$Source = 'upstream')
. "$PSScriptRoot/build-env.ps1"
if ($Source -eq 'upstream') { $env:CARGO_TARGET_DIR = Join-Path $ProjectRoot 'upstream/codex-rs/target' }
Push-Location (Join-Path $ProjectRoot "$Source/codex-rs")
try {
    & cargo build --locked --bin codex --bin codex-code-mode-host --bin codex-windows-sandbox-setup --bin codex-command-runner --bin codex-app-server 2>&1 | Tee-Object -FilePath (Join-Path $ProjectRoot "work/build-$Source.log")
    if ($LASTEXITCODE -ne 0) { throw "Cargo build failed: $LASTEXITCODE" }
    $destination = Join-Path $ProjectRoot "artifacts/$Source"
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    foreach ($name in @('codex','codex-code-mode-host','codex-windows-sandbox-setup','codex-command-runner','codex-app-server')) {
        Copy-Item -LiteralPath (Join-Path $env:CARGO_TARGET_DIR "debug/$name.exe") -Destination $destination
    }
    & (Join-Path $destination 'codex.exe') --version
    & python (Join-Path $PSScriptRoot 'stamp_build.py') $Source
    if ($LASTEXITCODE -ne 0) { throw 'Build artifact validation failed.' }
} finally { Pop-Location }
