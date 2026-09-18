$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:GITHUB_ENV = Join-Path $ProjectRoot 'work/build-env.txt'
[IO.File]::WriteAllText($env:GITHUB_ENV, '')
& (Join-Path $ProjectRoot 'upstream/.github/actions/setup-msvc-env/setup-msvc-env.ps1') -Target 'x86_64-pc-windows-msvc'
foreach ($line in [IO.File]::ReadAllLines($env:GITHUB_ENV)) {
    $parts = $line.Split('=', 2)
    if ($parts.Length -eq 2) { [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process') }
}
$env:CARGO_TARGET_DIR = Join-Path $ProjectRoot 'work/target-runtime'
$env:CARGO_BUILD_JOBS = '48'
$env:LIBSQLITE3_FLAGS = 'SQLITE_DISABLE_INTRINSIC'
$env:RUSTY_V8_ARCHIVE = Join-Path $ProjectRoot 'work/v8/rusty_v8_ptrcomp_sandbox_release_x86_64-pc-windows-msvc.lib.gz'
$env:RUSTY_V8_SRC_BINDING_PATH = Join-Path $ProjectRoot 'work/v8/src_binding_ptrcomp_sandbox_release_x86_64-pc-windows-msvc.rs'
$env:PATH = (Join-Path $ProjectRoot 'work/tools') + ';' + $env:PATH
