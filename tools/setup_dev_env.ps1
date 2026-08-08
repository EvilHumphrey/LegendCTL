<#
setup_dev_env.ps1 — bootstrap the .venv-zd Python venv used by tools\build_release.ps1.

Why this exists: build_release.ps1 expects an already-populated .venv-zd at the
repo root and aborts otherwise. The venv is one-time setup, but it can vanish
(disk wipe, antivirus quarantine, fresh checkout, accidental delete leaving an
empty directory). Without this script the recovery incantation
(`py312 -m venv .venv-zd` from a specific Python install path) lives only in
memory files — easy to forget and easy to get wrong. This script encodes the
canonical setup so any contributor can run one command and get a build-ready
venv.

What it does:
  1. Locate Python 3.12 at the expected install path. Exit non-zero with a
     clear hint if missing.
  2. If .venv-zd's recorded lock hash matches, do nothing; otherwise require
     explicit -Recreate before replacing a populated venv.
  3. If .venv-zd exists but is empty (stale shell, antivirus aftermath),
     log and recreate it.
  4. Download the complete release lock into a temporary hash-verified
     wheelhouse, then install it offline into .venv-zd.
  5. On pip failure, remove the half-populated .venv-zd so the next run
     starts clean.

After this script exits 0, `tools\build_release.ps1` can be run directly. The
build script verifies this setup marker and never resolves packages itself.
#>
[CmdletBinding()]
param(
    # Recreate a populated venv when requirements-release.lock changed. This
    # is explicit because replacing an existing local environment is destructive.
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    <#
    PS 5.1 wraps stderr from native exes (pip, py.exe) as NativeCommandError
    under ErrorActionPreference="Stop", aborting BEFORE $LASTEXITCODE can be
    checked. Mirror build_release.ps1's helper: temporarily switch to Continue,
    then explicitly check $LASTEXITCODE. Restored in finally so callers see
    no preference drift.
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

$py312 = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py312)) {
    Write-Error @"
Python 3.12 not found at expected path:
  $py312

Install Python 3.12 from https://www.python.org/downloads/release/python-3120/
(use the per-user installer; default install path matches the above).
"@
}

$venvDir    = Join-Path $repoRoot ".venv-zd"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$releaseLock = Join-Path $repoRoot "requirements-release.lock"
$lockMarker = Join-Path $venvDir ".legendctl-release-lock.sha256"

if (-not (Test-Path -LiteralPath $releaseLock)) {
    Write-Error "Missing requirements-release.lock. Refusing an unlocked release setup."
}
$lockHash = (Get-FileHash -LiteralPath $releaseLock -Algorithm SHA256).Hash.ToLowerInvariant()

if (Test-Path $venvPython) {
    $recordedHash = if (Test-Path -LiteralPath $lockMarker) { (Get-Content -LiteralPath $lockMarker -Raw).Trim() } else { "" }
    if ($recordedHash -eq $lockHash) {
        Write-Host "venv already matches requirements-release.lock. Nothing to do." -ForegroundColor Green
        exit 0
    }
    if (-not $Recreate) {
        Write-Error @"
The existing .venv-zd is not proven to match requirements-release.lock.
Run .\tools\setup_dev_env.ps1 -Recreate to replace it with a hash-locked offline build venv.
"@
    }
    Write-Host "Recreating .venv-zd at explicit request because its release-lock marker is missing or stale." -ForegroundColor Yellow
}

if (Test-Path $venvDir) {
    # Directory exists but Scripts\python.exe is missing — empty shell from a
    # prior wipe, partial extraction, or antivirus quarantine. Recreate it.
    Write-Host "venv directory exists but is empty (no Scripts\python.exe) — recreating." -ForegroundColor Yellow
    try {
        Remove-Item -Recurse -Force $venvDir
    } catch {
        Write-Error @"
Could not remove $venvDir : $_

A process may be holding files inside .venv-zd (open shell with the venv
activated, file explorer window, etc). Close any such process and re-run.
"@
    }
}

Write-Host "Creating venv at .venv-zd via Python 3.12..." -ForegroundColor Cyan
Invoke-NativeCommand -Label "py312 -m venv .venv-zd" -ScriptBlock { & $py312 -m venv $venvDir }

if (-not (Test-Path $venvPython)) {
    Write-Error "venv create reported success but $venvPython is missing."
}

$wheelhouse = Join-Path $repoRoot (".release-wheelhouse-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $wheelhouse | Out-Null

Write-Host "Downloading the hash-locked release wheelhouse..." -ForegroundColor Cyan
try {
    Invoke-NativeCommand -Label "pip download (requirements-release.lock)" -ScriptBlock {
        & $py312 -m pip download --disable-pip-version-check --only-binary=:all: --no-deps `
            --require-hashes --dest $wheelhouse -r $releaseLock
    }
    Write-Host "Installing the release toolchain offline..." -ForegroundColor Cyan
    Invoke-NativeCommand -Label "offline pip install (requirements-release.lock)" -ScriptBlock {
        & $venvPython -m pip install --disable-pip-version-check --no-index --find-links $wheelhouse `
            --only-binary=:all: --no-deps --require-hashes -r $releaseLock
    }
    Invoke-NativeCommand -Label "pip check (release venv)" -ScriptBlock {
        & $venvPython -m pip check
    }
    Set-Content -LiteralPath $lockMarker -Value $lockHash -Encoding ASCII -NoNewline
} catch {
    # Half-populated venv on pip failure — remove so next run starts clean.
    Write-Warning "pip install failed; removing partially-populated .venv-zd so retry is clean."
    try {
        Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
    } catch {
        Write-Warning "Could not remove partial venv at $venvDir : $_"
    }
    throw
} finally {
    if (Test-Path -LiteralPath $wheelhouse) {
        Remove-Item -LiteralPath $wheelhouse -Recurse -Force
    }
}

Write-Host ""
Write-Host "Setup complete. Run tools\build_release.ps1 to build a release." -ForegroundColor Green
