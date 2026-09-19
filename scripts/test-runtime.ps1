. "$PSScriptRoot/build-env.ps1"
$ErrorActionPreference = 'Continue'
# The justfile needs PowerShell 7 on PATH; the workspace ships a portable copy.
$portable = Join-Path $ProjectRoot 'work/tools/pwsh-7.5.3'
if (Test-Path -LiteralPath $portable) { $env:PATH = $portable + ';' + $env:PATH }
if (-not $env:CARGO_BUILD_JOBS -or [int]$env:CARGO_BUILD_JOBS -gt 8) { $env:CARGO_BUILD_JOBS = '4' }
$log = Join-Path $ProjectRoot 'work/targeted-tests.log'
$expression = '(package(codex-core) & (test(cross_provider_subagents) | test(agent::) | test(subagent_model_provider) | test(multi_agent) | test(client_common) | test(client_tests))) | not package(codex-core)'
Push-Location (Join-Path $ProjectRoot 'runtime/codex-rs')
try {
    & just test -p codex-core -p codex-agent-roles -p codex-model-provider -p codex-config --test-threads 8 -E $expression *> $log
    if ($LASTEXITCODE -ne 0) { throw "Targeted Rust tests failed. See $log" }
} finally { Pop-Location }
