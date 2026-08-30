# Intentionally explicit CI preparation; build_release.ps1 never downloads or
# installs a compiler on a developer's machine.
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') {
    throw 'This compiler acquisition script is for disposable GitHub Actions runners only.'
}
. (Join-Path $PSScriptRoot 'inno_setup.ps1')
$repoRoot = Split-Path -Parent $PSScriptRoot
$pin = Read-InnoSetupPin
$null = Assert-InnoLocalPath -Path $repoRoot
$toolsDir = Join-Path $repoRoot '.release-tools'
if (-not (Test-Path -LiteralPath $toolsDir)) {
    New-Item -ItemType Directory -Path $toolsDir | Out-Null
}
$null = Assert-InnoLocalPath -Path $toolsDir
$compilerDir = Get-InnoSetupDirectory -RepoRoot $repoRoot -Pin $pin
if (Test-Path -LiteralPath $compilerDir) {
    throw 'Refusing to overlay an existing Inno Setup compiler directory.'
}
$downloadDir = Join-Path $toolsDir ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $downloadDir | Out-Null
$installer = Join-Path $downloadDir "innosetup-$($pin.version).exe"
Invoke-WebRequest -UseBasicParsing -Uri $pin.installer.url -OutFile $installer -ErrorAction Stop
# Keep the verified installer open without write/delete sharing through use.
$installerLock = [IO.File]::Open($installer, 'Open', 'Read', 'Read')
try {
    Assert-InnoSetupInstaller -Path $installer -Pin $pin
    # Portable mode disables uninstall registration, file associations and
    # shortcuts. The reviewed upstream 6.7.1 isportable.iss defines this mode.
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                   '/CURRENTUSER', '/PORTABLE=1', ('/DIR="' + $compilerDir + '"'))
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -WorkingDirectory $downloadDir -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Pinned Inno Setup preparation failed (exit $($process.ExitCode))."
    }
} finally {
    $installerLock.Dispose()
}
$null = Assert-InnoSetupCompiler -Directory $compilerDir -Pin $pin
Write-Host "Inno Setup $($pin.version) compiler closure verified."
