$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/build-env.ps1"
Push-Location (Join-Path $ProjectRoot 'runtime/codex-rs')
try {
    & just test -p codex-core -p codex-agent-roles -p codex-model-provider -p codex-config --test-threads 8 -E '(package(codex-core) & (test(cross_provider_subagents) | test(agent::) | test(subagent_model_provider) | test(multi_agent) | test(client_common) | test(client_tests))) | not package(codex-core)' 2>&1 | Tee-Object -FilePath (Join-Path $ProjectRoot 'work/targeted-tests.log')
    if ($LASTEXITCODE -ne 0) { throw 'Targeted Rust tests failed. See work/targeted-tests.log.' }
} finally { Pop-Location }
