<#
setup_dev_env.ps1 -- create the hash-locked .venv-zd used by build_release.ps1.

This script never repairs or recursively deletes an existing .venv-zd. A stale,
incomplete, or redirected environment is retained for inspection and requires
manual recovery. New environments are marked incomplete until the offline,
hash-checked installation and pip check have both succeeded.
#>
[CmdletBinding()]
param(
    # Kept so old invocations fail with guidance instead of silently changing meaning.
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][scriptblock]$ScriptBlock
    )
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $ScriptBlock
        if ($LASTEXITCODE -ne 0) { throw "${Label} failed (exit $LASTEXITCODE)" }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Get-EntryAttributesNoFollow {
    param([Parameter(Mandatory)][string]$LiteralPath)
    return [IO.File]::GetAttributes($LiteralPath)
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$LiteralPath)
    $stream = [IO.File]::Open($LiteralPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
}

function Assert-OrdinaryDirectoryEntry {
    param([Parameter(Mandatory)][string]$LiteralPath)
    $attributes = Get-EntryAttributesNoFollow -LiteralPath $LiteralPath
    if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($attributes -band [IO.FileAttributes]::Directory) -eq 0) {
        throw "Expected an ordinary directory, without redirects: $LiteralPath"
    }
}

function Assert-OrdinaryFileEntry {
    param([Parameter(Mandatory)][string]$LiteralPath)
    $attributes = Get-EntryAttributesNoFollow -LiteralPath $LiteralPath
    if (($attributes -band ([IO.FileAttributes]::ReparsePoint -bor [IO.FileAttributes]::Directory)) -ne 0) {
        throw "Expected an ordinary file, without redirects: $LiteralPath"
    }
}

function Assert-NoReparseTree {
    param([Parameter(Mandatory)][string]$LiteralPath)
    Assert-OrdinaryDirectoryEntry -LiteralPath $LiteralPath
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push([IO.Path]::GetFullPath($LiteralPath))
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $attributes = Get-EntryAttributesNoFollow -LiteralPath $entry
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Environment contains a redirect and is unsafe to execute: $entry"
            }
            if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) { $pending.Push($entry) }
        }
    }
}

function Throw-ManualRecoveryRequired {
    param([Parameter(Mandatory)][string]$Reason)
    throw @"
$Reason

LegendCTL will not delete or repair an existing .venv-zd automatically. Close
processes using it, inspect it, then move it aside manually to a path outside
this checkout. Re-run tools\setup_dev_env.ps1 only after .venv-zd,
.venv-zd.setup-lock, and .venv-zd.setup-incomplete are absent. Do not delete an
unknown redirect.
"@
}

function Remove-WheelhouseSafely {
    param(
        [Parameter(Mandatory)][string]$Wheelhouse,
        [Parameter(Mandatory)][string]$RepositoryRoot
    )
    if (-not [IO.Directory]::Exists($Wheelhouse)) { return }
    $emptySource = Join-Path $RepositoryRoot (".release-wheelhouse-clean-" + [Guid]::NewGuid().ToString("N"))
    [IO.Directory]::CreateDirectory($emptySource) | Out-Null
    try {
        . (Join-Path $PSScriptRoot "release_paths.ps1")
        if (-not ("LegendCtlReleaseMirror" -as [type])) {
            Add-Type -Path (Join-Path $PSScriptRoot "release_mirror.cs")
        }
        Assert-ReleasePath -LiteralPath $emptySource -Within $RepositoryRoot -Recurse -RejectHardLinks
        Assert-ReleasePath -LiteralPath $Wheelhouse -Within $RepositoryRoot -Recurse -RejectHardLinks
        $mirror = [LegendCtlReleaseMirror]::new($emptySource, $Wheelhouse)
        try {
            $mirror.Prepare()
            $mirror.Publish()
        } finally {
            $mirror.Dispose()
        }
        # Both roots are verified empty. Non-recursive deletion cannot walk a replacement.
        [IO.Directory]::Delete($Wheelhouse, $false)
    } finally {
        if ([IO.Directory]::Exists($emptySource)) { [IO.Directory]::Delete($emptySource, $false) }
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$py312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$venvDir = Join-Path $repoRoot ".venv-zd"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$releaseLock = Join-Path $repoRoot "requirements-release.lock"
$lockMarker = Join-Path $venvDir ".legendctl-release-lock.sha256"
$setupLock = Join-Path $repoRoot ".venv-zd.setup-lock"
$incompleteMarker = Join-Path $repoRoot ".venv-zd.setup-incomplete"

if ($Recreate) {
    Throw-ManualRecoveryRequired -Reason "-Recreate is no longer supported because automatic replacement was unsafe."
}
if (-not [IO.File]::Exists($py312)) { Write-Error "Python 3.12 not found at expected path: $py312" }
$setupLockStream = $null
$incompleteToken = [Guid]::NewGuid().ToString("N")
$wheelhouse = $null
$savedPipEnvironment = @{}
try {
    try {
        $setupLockStream = [IO.File]::Open($setupLock, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        Throw-ManualRecoveryRequired -Reason "Another setup acquired the exclusive setup lock: $setupLock"
    }

    # Every lifecycle decision is made after the exclusive lock is held. A
    # delayed contender must not reuse an earlier observation that the venv was
    # absent after another setup has completed.
    if (-not [IO.File]::Exists($releaseLock)) {
        Write-Error "Missing requirements-release.lock. Refusing an unlocked release setup."
    }
    Assert-OrdinaryFileEntry -LiteralPath $releaseLock
    $lockHash = Get-Sha256Hex -LiteralPath $releaseLock

    if ([IO.File]::Exists($incompleteMarker) -or [IO.Directory]::Exists($incompleteMarker)) {
        Throw-ManualRecoveryRequired -Reason "A prior setup did not complete: $incompleteMarker"
    }

    $venvAlreadyReady = $false
    if ([IO.Directory]::Exists($venvDir) -or [IO.File]::Exists($venvDir)) {
        try {
            Assert-NoReparseTree -LiteralPath $venvDir
            Assert-OrdinaryFileEntry -LiteralPath $venvPython
            Assert-OrdinaryFileEntry -LiteralPath $lockMarker
            $recordedHash = ([IO.File]::ReadAllText($lockMarker)).Trim()
        } catch {
            Throw-ManualRecoveryRequired -Reason "The existing .venv-zd is incomplete or unsafe to execute: $($_.Exception.Message)"
        }
        if ($recordedHash -ne $lockHash) {
            Throw-ManualRecoveryRequired -Reason "The existing .venv-zd does not match requirements-release.lock."
        }
        $venvAlreadyReady = $true
    }

    if ($venvAlreadyReady) {
        Write-Host "venv already matches requirements-release.lock. Nothing to do." -ForegroundColor Green
    } else {
        $incompleteStream = [IO.File]::Open($incompleteMarker, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $incompleteBytes = [Text.Encoding]::ASCII.GetBytes($incompleteToken)
            $incompleteStream.Write($incompleteBytes, 0, $incompleteBytes.Length)
            $incompleteStream.Flush($true)
        } finally {
            $incompleteStream.Dispose()
        }

        Write-Host "Creating venv at .venv-zd via Python 3.12..." -ForegroundColor Cyan
        Invoke-NativeCommand -Label "py312 -m venv .venv-zd" -ScriptBlock { & $py312 -m venv $venvDir }
        Assert-NoReparseTree -LiteralPath $venvDir
        Assert-OrdinaryFileEntry -LiteralPath $venvPython

        $wheelhouse = Join-Path $repoRoot (".release-wheelhouse-" + [Guid]::NewGuid().ToString("N"))
        [IO.Directory]::CreateDirectory($wheelhouse) | Out-Null
        Assert-OrdinaryDirectoryEntry -LiteralPath $wheelhouse
        foreach ($entry in Get-ChildItem Env: | Where-Object { $_.Name -like "PIP_*" }) {
            $savedPipEnvironment[$entry.Name] = $entry.Value
            Remove-Item -LiteralPath ("Env:" + $entry.Name)
        }

        Write-Host "Downloading the hash-locked release wheelhouse..." -ForegroundColor Cyan
        Invoke-NativeCommand -Label "pip download (requirements-release.lock)" -ScriptBlock {
            & $py312 -m pip --isolated download --disable-pip-version-check --only-binary=:all: --no-deps `
                --require-hashes --dest $wheelhouse -r $releaseLock
        }
        Write-Host "Installing the release toolchain offline..." -ForegroundColor Cyan
        Invoke-NativeCommand -Label "offline pip install (requirements-release.lock)" -ScriptBlock {
            & $venvPython -m pip --isolated install --disable-pip-version-check --no-index --find-links $wheelhouse `
                --only-binary=:all: --no-deps --require-hashes -r $releaseLock
        }
        Invoke-NativeCommand -Label "pip check (release venv)" -ScriptBlock {
            & $venvPython -m pip --isolated check
        }

        Assert-NoReparseTree -LiteralPath $venvDir
        Assert-OrdinaryFileEntry -LiteralPath $venvPython
        $markerStream = [IO.File]::Open($lockMarker, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $markerBytes = [Text.Encoding]::ASCII.GetBytes($lockHash)
            $markerStream.Write($markerBytes, 0, $markerBytes.Length)
            $markerStream.Flush($true)
        } finally {
            $markerStream.Dispose()
        }
        Remove-WheelhouseSafely -Wheelhouse $wheelhouse -RepositoryRoot $repoRoot
        $wheelhouse = $null
        if ([IO.File]::ReadAllText($incompleteMarker).Trim() -ne $incompleteToken) {
            throw "The setup-incomplete marker changed during setup; refusing to remove it."
        }
        [IO.File]::Delete($incompleteMarker)
        Write-Host "Setup complete. Run tools\build_release.ps1 to build a release." -ForegroundColor Green
    }
} catch {
    Write-Warning "Setup failed. .venv-zd and .venv-zd.setup-incomplete were retained for inspection."
    throw
} finally {
    foreach ($name in $savedPipEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedPipEnvironment[$name], "Process")
    }
    if ($wheelhouse -and [IO.Directory]::Exists($wheelhouse)) {
        try {
            Remove-WheelhouseSafely -Wheelhouse $wheelhouse -RepositoryRoot $repoRoot
        } catch {
            Write-Warning "Safe wheelhouse cleanup refused or failed; retained for inspection: $wheelhouse ($($_.Exception.Message))"
        }
    }
    if ($setupLockStream) {
        $setupLockStream.Dispose()
        [IO.File]::Delete($setupLock)
    }
}
