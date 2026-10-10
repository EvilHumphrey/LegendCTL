[CmdletBinding()]
param(
    [string]$ArtifactDestination,
    # Internal worker: invoked only from the raw-blob export orchestrator.
    [switch]$FromVerifiedSnapshot,
    [string]$BuildPython,
    [string]$ExpectedCommit
)

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
    fixed) but remains correct -- belt-and-suspenders, no regression.

    .PARAMETER Label
    Short description used in the throw message (e.g. "pip install", "PyInstaller").

    .PARAMETER ScriptBlock
    The native invocation, e.g. { & $python -I -m pip install -r requirements-build.txt }.
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

$python = if ($FromVerifiedSnapshot) { $BuildPython } else { Join-Path $repoRoot ".venv-zd\Scripts\python.exe" }
if (-not $FromVerifiedSnapshot) {
    if ($BuildPython -or $ExpectedCommit) { throw "Snapshot parameters are internal to the release builder." }
    if (-not $ArtifactDestination) { $ArtifactDestination = Join-Path $repoRoot "dist" }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Expected venv at .venv-zd; run tools\setup_dev_env.ps1 first."
    }
    Invoke-NativeCommand -Label "commit-bound release build" -ScriptBlock { & $python -I -S (Join-Path $PSScriptRoot "export_git_tree.py") --repo $repoRoot --destination $ArtifactDestination --python $python }
    return
}
if ($ArtifactDestination -or $ExpectedCommit -notmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$' -or -not [IO.Path]::IsPathRooted($BuildPython)) {
    throw "Invalid isolated build identity or toolchain."
}
if (Test-Path -LiteralPath (Join-Path $repoRoot ".git")) {
    throw "The internal worker requires a raw export without Git checkout metadata."
}
if (-not (Test-Path $python)) {
    Write-Error "Expected venv at .venv-zd; run ``tools\setup_dev_env.ps1`` first."
}
$releaseLock = Join-Path $repoRoot "requirements-release.lock"
$lockMarker = Join-Path (Split-Path -Parent (Split-Path -Parent $python)) ".legendctl-release-lock.sha256"
if (-not (Test-Path -LiteralPath $releaseLock) -or -not (Test-Path -LiteralPath $lockMarker)) {
    Write-Error "Expected a hash-locked release venv; run ``tools\setup_dev_env.ps1`` first."
}
$expectedLockHash = (Get-FileHash -LiteralPath $releaseLock -Algorithm SHA256).Hash.ToLowerInvariant()
$recordedLockHash = (Get-Content -LiteralPath $lockMarker -Raw).Trim()
# Existing setup markers hash checkout bytes. A text checkout may have CRLF
# while this raw Git export has LF. Accept only exact raw/LF/CRLF encodings of
# this same lock; every pin, wheel hash and other character stays unchanged.
$lockEncoding = New-Object System.Text.UTF8Encoding($false, $true)
$lockText = $lockEncoding.GetString([IO.File]::ReadAllBytes($releaseLock))
$lfLockText = $lockText.Replace("`r`n", "`n")
$acceptedLockHashes = @($expectedLockHash)
$lockHasher = [Security.Cryptography.SHA256]::Create()
try {
    foreach ($variant in @($lfLockText, $lfLockText.Replace("`n", "`r`n"))) {
        $acceptedLockHashes += [BitConverter]::ToString($lockHasher.ComputeHash($lockEncoding.GetBytes($variant))).Replace("-", "").ToLowerInvariant()
    }
} finally {
    $lockHasher.Dispose()
}
if ($recordedLockHash -notin $acceptedLockHashes) {
    Write-Error "The release venv does not match requirements-release.lock; run ``tools\setup_dev_env.ps1 -Recreate``."
}

$versionPy = Get-Content "zd_app\version.py" -Raw
if ($versionPy -match '__version__\s*=\s*"([^"]+)"') {
    $version = $matches[1]
} else {
    Write-Error "Could not parse __version__ from zd_app\version.py"
}
if ($version -notmatch '^\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)$') {
    throw "Version is not a safe artifact filename component."
}

$buildCommit = $ExpectedCommit
$buildDate = Get-Date -Format "yyyy-MM-dd"
$patchedVersionPy = $versionPy -replace '__build_commit__\s*=\s*"[^"]*"', "__build_commit__ = `"$buildCommit`""
if ($patchedVersionPy -eq $versionPy -and $versionPy -notmatch '__build_commit__') {
    $patchedVersionPy = $patchedVersionPy.TrimEnd() + "`r`n__build_commit__ = `"$buildCommit`"`r`n"
}
$patchedVersionPy = $patchedVersionPy -replace '__build_date__\s*=\s*"[^"]*"', "__build_date__ = `"$buildDate`""
if ($patchedVersionPy -notmatch '__build_date__') {
    $patchedVersionPy = $patchedVersionPy.TrimEnd() + "`r`n__build_date__ = `"$buildDate`"`r`n"
}
# PS5.1's Set-Content -Encoding UTF8 emits a BOM, which ast.parse(str) rejects.
# WriteAllText preserves the supplied newline text and is BOM-free on PS5/PS7.
$versionEncoding = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $repoRoot "zd_app\version.py"), $patchedVersionPy, $versionEncoding)

Write-Host "Building ZDUltimateLegend v$version" -ForegroundColor Cyan

try {
    # setup_dev_env.ps1 already installed this exact release lock from a
    # hash-verified wheelhouse. Do not run pip install here: a second resolver
    # would undermine the offline artifact-build boundary.
    Invoke-NativeCommand -Label "pip check (hash-locked release venv)" -ScriptBlock { & $python -I -m pip check }
    Invoke-NativeCommand -Label "pip-audit (hash-locked release venv)" -ScriptBlock { & $python -I -m pip_audit }

    if ((Test-Path -LiteralPath "build") -or (Test-Path -LiteralPath "dist")) {
        throw "Isolated source unexpectedly contains build outputs. Refusing replacement."
    }

    Invoke-NativeCommand -Label "PyInstaller" -ScriptBlock { & $python -I -m PyInstaller --noconfirm pyinstaller_main_zd.spec }

    Invoke-NativeCommand -Label "trust manifest" -ScriptBlock { & $python -I tools\generate_trust_manifest.py --dist-root dist\ZDUltimateLegend --repo-root . --source-commit $ExpectedCommit }

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
    $inno = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $inno)) {
        $inno = "C:\Program Files\Inno Setup 6\ISCC.exe"
    }
    if (-not (Test-Path $inno)) {
        Write-Warning "Inno Setup compiler (ISCC.exe) not found. Skipping installer build. Install Inno Setup 6.7.1+ from https://jrsoftware.org/isdl.php to enable installer output."
    } else {
        $env:ZDUL_VERSION = $version
        $issPath = Join-Path $repoRoot "tools\installer\inno_setup_zd_wrapper.iss"
        Write-Host "Building Inno Setup installer..." -ForegroundColor Cyan
        Invoke-NativeCommand -Label "Inno Setup compile" -ScriptBlock { & $inno $issPath }
        $installerPath = Join-Path $repoRoot "dist\ZDUltimateLegend-v$version-Setup.exe"
        if (Test-Path $installerPath) {
            $installerHash = (Get-FileHash $installerPath -Algorithm SHA256).Hash
        } else {
            throw "Inno Setup reported success but installer not found at $installerPath"
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
    Write-Host "Isolated artifacts ready for provenance validation:" -ForegroundColor Cyan
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
    # Keep the stamped source with the isolated diagnostics. The operator's
    # checkout was never edited, so there is nothing there to roll back.
    Set-Location $repoRoot
}
