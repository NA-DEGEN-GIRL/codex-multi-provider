# Build a separate candidate. This never changes the runtime of an open profile.
. "$PSScriptRoot/build-env.ps1"
Push-Location (Join-Path $ProjectRoot 'runtime/codex-rs')
try {
    & cargo build --locked --bin codex --bin codex-code-mode-host --bin codex-windows-sandbox-setup --bin codex-command-runner --bin codex-app-server
    if ($LASTEXITCODE -ne 0) { throw "Candidate build failed: $LASTEXITCODE" }
    & 'C:\Python313\python.exe' (Join-Path $PSScriptRoot 'stage_manager_runtime.py')
    if ($LASTEXITCODE -ne 0) { throw 'Candidate staging failed.' }
} finally { Pop-Location }
