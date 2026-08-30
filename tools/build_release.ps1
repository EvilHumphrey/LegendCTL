$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    <#
    .SYNOPSIS
    Invoke a native executable so PS 5.1's strict ErrorActionPreference="Stop"
    doesn't wrap its stderr as a NativeCommandError and abort the calling try block.

    .DESCRIPTION
    PowerShell 5.1 wraps stderr from native executables (pip, PyInstaller, ISCC)
    as ErrorRecord (NativeCommandError) when ErrorActionPreference is "Stop",
    aborting the script BEFORE $LASTEXITCODE can be checked. This helper temporarily
    sets ErrorActionPreference to "Continue" around the native call, then explicitly
    checks $LASTEXITCODE and throws on non-zero. Restores the prior preference
    in finally so caller-scope behaviour is unchanged. The explicit exit-code check
    is a no-op-equivalent override on PS 7+ (where the wrap-as-ErrorRecord bug was
    fixed) but remains correct — belt-and-suspenders, no regression.

    .PARAMETER Label
    Short description used in the throw message (e.g. "pip install", "PyInstaller").

    .PARAMETER ScriptBlock
    The native invocation, e.g. { & $python -m pip install -r requirements-build.txt }.
    #>
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][scriptblock]$ScriptBlock
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $ScriptBlock
        if ($LASTEXITCODE -ne 0) {
            throw "${Label} failed (exit $LASTEXITCODE)"
        }
    } finally {
        $ErrorActionPreference = $prev
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
. (Join-Path $PSScriptRoot 'inno_setup.ps1')
# Fail before packaging if an installed compiler is present but unapproved.
# This never executes a banner or trusts an installed-version registry value.
$inno = Find-VerifiedInnoSetupCompiler -RepoRoot $repoRoot

$python = Join-Path $repoRoot ".venv-zd\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Expected venv at .venv-zd; run ``tools\setup_dev_env.ps1`` first."
}
$releaseLock = Join-Path $repoRoot "requirements-release.lock"
$lockMarker = Join-Path $repoRoot ".venv-zd\.legendctl-release-lock.sha256"
if (-not (Test-Path -LiteralPath $releaseLock) -or -not (Test-Path -LiteralPath $lockMarker)) {
    Write-Error "Expected a hash-locked release venv; run ``tools\setup_dev_env.ps1`` first."
}
$expectedLockHash = (Get-FileHash -LiteralPath $releaseLock -Algorithm SHA256).Hash.ToLowerInvariant()
$recordedLockHash = (Get-Content -LiteralPath $lockMarker -Raw).Trim()
if ($recordedLockHash -ne $expectedLockHash) {
    Write-Error "The release venv does not match requirements-release.lock; run ``tools\setup_dev_env.ps1 -Recreate``."
}

$versionPy = Get-Content "zd_app\version.py" -Raw
if ($versionPy -match '__version__\s*=\s*"([^"]+)"') {
    $version = $matches[1]
} else {
    Write-Error "Could not parse __version__ from zd_app\version.py"
}

$buildCommit = (& git rev-parse --short HEAD).Trim()
if (-not $buildCommit) {
    $buildCommit = "unknown"
}
$buildDate = Get-Date -Format "yyyy-MM-dd"
$originalVersionPy = $versionPy
$patchedVersionPy = $versionPy -replace '__build_commit__\s*=\s*"[^"]*"', "__build_commit__ = `"$buildCommit`""
if ($patchedVersionPy -eq $versionPy -and $versionPy -notmatch '__build_commit__') {
    $patchedVersionPy = $patchedVersionPy.TrimEnd() + "`r`n__build_commit__ = `"$buildCommit`"`r`n"
}
$patchedVersionPy = $patchedVersionPy -replace '__build_date__\s*=\s*"[^"]*"', "__build_date__ = `"$buildDate`""
if ($patchedVersionPy -notmatch '__build_date__') {
    $patchedVersionPy = $patchedVersionPy.TrimEnd() + "`r`n__build_date__ = `"$buildDate`"`r`n"
}
Set-Content "zd_app\version.py" -Value $patchedVersionPy -Encoding UTF8 -NoNewline

Write-Host "Building ZDUltimateLegend v$version" -ForegroundColor Cyan

try {
    # setup_dev_env.ps1 already installed this exact release lock from a
    # hash-verified wheelhouse. Do not run pip install here: a second resolver
    # would undermine the offline artifact-build boundary.
    Invoke-NativeCommand -Label "pip check (hash-locked release venv)" -ScriptBlock { & $python -m pip check }
    Invoke-NativeCommand -Label "pip-audit (hash-locked release venv)" -ScriptBlock { & $python -m pip_audit }

    if (Test-Path "build") {
        Remove-Item -Recurse -Force "build"
    }
    if (Test-Path "dist") {
        Remove-Item -Recurse -Force "dist"
    }

    Invoke-NativeCommand -Label "PyInstaller" -ScriptBlock { & $python -m PyInstaller --noconfirm pyinstaller_main_zd.spec }

    Invoke-NativeCommand -Label "trust manifest" -ScriptBlock { & $python tools\generate_trust_manifest.py --dist-root dist\ZDUltimateLegend --repo-root . }

    $srcDir = Join-Path $repoRoot "dist\ZDUltimateLegend"
    $dstDir = Join-Path $repoRoot "dist\ZDUltimateLegend-v$version"
    if (-not (Test-Path $srcDir)) {
        Write-Error "Expected PyInstaller output at $srcDir"
    }
    Move-Item $srcDir $dstDir

    $zipPath = Join-Path $repoRoot "dist\ZDUltimateLegend-v$version-windows.zip"
    Compress-Archive -Path $dstDir -DestinationPath $zipPath -Force

    $hash = Get-FileHash $zipPath -Algorithm SHA256

    # --- Build Inno Setup installer ---
    $installerPath = $null
    $installerHash = $null
    if (-not $inno) {
        Write-Warning "Inno Setup compiler (ISCC.exe) not found. Skipping installer build. See docs/inno-setup-toolchain.md for the exact approved compiler."
    } else {
        $env:ZDUL_VERSION = $version
        $issPath = Join-Path $repoRoot "tools\installer\inno_setup_zd_wrapper.iss"
        # Recheck the same selected closure at the execution boundary after
        # the longer Python packaging phase; do not rediscover/fall back.
        $inno = Assert-InnoSetupCompiler -Directory (Split-Path -Parent $inno) -Pin (Read-InnoSetupPin)
        Write-Host "Building Inno Setup installer..." -ForegroundColor Cyan
        Invoke-NativeCommand -Label "Inno Setup compile" -ScriptBlock { & $inno $issPath }
        $installerPath = Join-Path $repoRoot "dist\ZDUltimateLegend-v$version-Setup.exe"
        if (Test-Path $installerPath) {
            $installerHash = (Get-FileHash $installerPath -Algorithm SHA256).Hash
        } else {
            Write-Warning "Inno Setup reported success but installer not found at $installerPath"
        }
    }

    # --- SHA256SUMS.txt artifact (publishable alongside release downloads) ---
    # The ZIP (and installer, when built) are already hashed above; persist
    # them to a standard `<hash>  <file>` manifest next to the build so release
    # uploads can ship verifiable checksums.
    $sumsPath = Join-Path $repoRoot "dist\SHA256SUMS.txt"
    $sumsLines = @("$($hash.Hash)  $(Split-Path $zipPath -Leaf)")
    if ($installerPath -and $installerHash -and (Test-Path $installerPath)) {
        $sumsLines += "$installerHash  $(Split-Path $installerPath -Leaf)"
    }
    Set-Content -Path $sumsPath -Value $sumsLines -Encoding UTF8

    Write-Host ""
    Write-Host "Build complete:" -ForegroundColor Green
    Write-Host "  Folder:    $dstDir"
    Write-Host "  Zip:       $zipPath"
    Write-Host "  SHA256:    $($hash.Hash)"
    if ($installerPath -and (Test-Path $installerPath)) {
        Write-Host "  Installer: $installerPath"
        if ($installerHash) {
            Write-Host "  SHA256:    $installerHash"
        }
    }
    Write-Host "  Checksums: $sumsPath"
} finally {
    Set-Content "zd_app\version.py" -Value $originalVersionPy -Encoding UTF8 -NoNewline
}

# --- Refresh the local install + Desktop/Start Menu shortcuts to this build ---
# dist\ is wiped + versioned on every build, so the operator's clickable
# shortcuts can't target it directly. install_local.ps1 mirrors this fresh build
# to <repo>\local-install\ and re-points the shortcuts at it. Guarded so a
# shortcut hiccup never makes a successful build look failed.
try {
    Write-Host ""
    Write-Host "Refreshing local install + Desktop/Start Menu shortcuts..." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "install_local.ps1")
} catch {
    Write-Warning "Build succeeded but shortcut refresh failed: $_"
    Write-Warning "Re-run tools\install_local.ps1 manually (close the app first if it's open)."
}
