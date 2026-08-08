<#
.SYNOPSIS
Refresh the hash-locked Python closure used by LegendCTL release builds.

.DESCRIPTION
The release path never calls this script. It consumes requirements-release.lock
from a temporary, hash-verified wheelhouse and therefore cannot perform a live
resolution while it builds or audits an artifact. This maintainer command is the
one deliberate place that resolves the human-maintained roots in
requirements-release.in, then writes the reviewed lock snapshot.

It bootstraps pip-tools from requirements-lock-tools.lock, which is itself
version- and hash-locked, and proves the resulting release lock can download a
complete wheel set with --require-hashes. Review the resulting lock diff before
committing it. The temporary directories are removed on both success and error.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][scriptblock]$ScriptBlock
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $ScriptBlock
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed (exit $LASTEXITCODE)"
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$py312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path -LiteralPath $py312)) {
    throw "Python 3.12 is required at $py312 to refresh the release lock."
}

$temporaryRoot = Join-Path $repoRoot (".release-lock-refresh-" + [Guid]::NewGuid().ToString("N"))
$toolWheelhouse = Join-Path $temporaryRoot "tool-wheelhouse"
$releaseWheelhouse = Join-Path $temporaryRoot "release-wheelhouse"
$toolVenv = Join-Path $temporaryRoot "tool-venv"
$toolPython = Join-Path $toolVenv "Scripts\python.exe"
$savedPipEnvironment = @{}

# pip's --isolated ignores user config and ordinary PIP_* settings, but pip
# reads PIP_CONFIG_FILE before that mode can take effect. Remove every inherited
# PIP_* variable for this process as well; restore them in finally so an invoked
# script scope never leaks an environment change back to a calling session.
Get-ChildItem Env: | Where-Object { $_.Name -like 'PIP_*' } | ForEach-Object {
    $savedPipEnvironment[$_.Name] = $_.Value
    Remove-Item -LiteralPath ("Env:\" + $_.Name)
}

try {
    New-Item -ItemType Directory -Path $toolWheelhouse, $releaseWheelhouse | Out-Null

    # Bootstrap only from the committed, hash-locked tooling closure.
    Invoke-NativeCommand -Label "download hash-locked pip-tools wheelhouse" -ScriptBlock {
        & $py312 -m pip --isolated download --disable-pip-version-check --only-binary=:all: --no-deps `
            --require-hashes --dest $toolWheelhouse -r requirements-lock-tools.lock
    }
    Invoke-NativeCommand -Label "create lock tooling venv" -ScriptBlock {
        & $py312 -m venv $toolVenv
    }
    Invoke-NativeCommand -Label "install hash-locked pip-tools offline" -ScriptBlock {
        & $toolPython -m pip install --disable-pip-version-check --no-index --find-links $toolWheelhouse `
            --only-binary=:all: --no-deps --require-hashes -r requirements-lock-tools.lock
    }
    Invoke-NativeCommand -Label "check lock tooling closure" -ScriptBlock {
        & $toolPython -m pip check
    }

    # This is the intentional live resolution. --no-config disables pip-tools'
    # own config, while the forwarded --isolated disables pip configuration and
    # PIP_* environment variables inside the resolver. Pair it with wheel-only
    # resolution so pip-tools cannot select an sdist or execute a build backend
    # before the later wheelhouse proof. The command remains explicit and
    # deterministic for the pinned roots, and writes hashes fetched from PyPI.
    Invoke-NativeCommand -Label "compile release dependency lock" -ScriptBlock {
        & $toolPython -m piptools compile --no-config --allow-unsafe --generate-hashes --strip-extras `
            --pip-args '--isolated --only-binary=:all:' `
            --output-file requirements-release.lock requirements-release.in
    }
    # pip-tools retains --pip-args in its generated command echo, but omits
    # its own --no-config flag. Stamp that intentionally omitted safety input
    # into the echo so the committed lock truthfully records both policies.
    $lockContents = Get-Content -LiteralPath requirements-release.lock -Raw
    if ($lockContents -notmatch '(?m)^#    pip-compile --no-config ') {
        $lockContents = $lockContents -replace '(?m)^(#    pip-compile )', '$1--no-config '
        # Use .NET rather than a PowerShell-specific encoding name: this script
        # supports Windows PowerShell 5.1 as well as pwsh.
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText((Join-Path $repoRoot 'requirements-release.lock'), $lockContents, $utf8NoBom)
    }

    # Prove all selected wheels are present on PyPI and satisfy the generated
    # SHA-256 entries. --no-deps prevents a hidden second resolver from adding
    # a package that is absent from the reviewed lock.
    Invoke-NativeCommand -Label "verify release lock wheel closure" -ScriptBlock {
        & $py312 -m pip --isolated download --disable-pip-version-check --only-binary=:all: --no-deps `
            --require-hashes --dest $releaseWheelhouse -r requirements-release.lock
    }

    Write-Host "Release lock refreshed and hash-verified. Review requirements-release.lock before committing." -ForegroundColor Green
} finally {
    foreach ($name in $savedPipEnvironment.Keys) {
        Set-Item -LiteralPath ("Env:\" + $name) -Value $savedPipEnvironment[$name]
    }
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
