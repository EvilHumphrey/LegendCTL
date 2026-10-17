<#
.SYNOPSIS
Compile and optionally run the real Inno Pascal destination helper without installing.
.DESCRIPTION
Requires the reviewed compiler helper/lock used by build_release.ps1. Nothing is
downloaded or installed. The native fixture is lowest-privilege, has no payload,
registry, shortcuts, or uninstaller, and always aborts in InitializeSetup. Its
Windows filesystem cases live below EvidenceDirectory and are retained for review.
Run on the project's serialized hidden desktop when required by local guidance.

-BuildVmFixtures additionally compiles harmless marker payloads from unchanged
production .iss sources. These installers are NEVER executed by this script:
they retain production privileges/AppId and belong only in a disposable VM.

Examples:
  pwsh -File tools/test_installer_destination.ps1 -EvidenceDirectory .test-evidence -RunNativePolicy
  pwsh -File tools/test_installer_destination.ps1 -EvidenceDirectory .test-evidence -BuildVmFixtures -BaselineRef <reviewed-base-sha>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EvidenceDirectory,
    [string]$CompilerDirectory,
    [string]$CompilerHelperPath = (Join-Path $PSScriptRoot 'inno_setup.ps1'),
    [switch]$RunNativePolicy,
    [switch]$BuildVmFixtures,
    [string]$BaselineRef
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$policySource = Join-Path $PSScriptRoot 'installer/install_directory_policy.iss'
$installerSource = Join-Path $PSScriptRoot 'installer/inno_setup_zd_wrapper.iss'
if ($BuildVmFixtures -and $BaselineRef -notmatch '^[0-9a-fA-F]{40}$') {
    throw 'BuildVmFixtures requires an explicit full reviewed BaselineRef SHA.'
}
. $CompilerHelperPath
$pin = Read-InnoSetupPin
if ($CompilerDirectory) {
    $compiler = Assert-InnoSetupCompiler -Directory $CompilerDirectory -Pin $pin
} else {
    $compiler = Find-VerifiedInnoSetupCompiler -RepoRoot $repoRoot
}
if (-not $compiler) { throw 'Reviewed Inno compiler unavailable; native verification was NOT run.' }

$evidenceRoot = [IO.Path]::GetFullPath($EvidenceDirectory)
$runRoot = Join-Path $evidenceRoot ('run-' + [guid]::NewGuid().ToString('N').Substring(0, 10))
$nativeRoot = Join-Path $runRoot 'native'
$tempRoot = Join-Path $runRoot 'temp'
$null = New-Item -ItemType Directory -Path $nativeRoot, $tempRoot -Force
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, $utf8)
}
function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Compile-Fixture([string]$ScriptPath, [string]$LogPath) {
    # Check the full reviewed compiler tree immediately before every invocation.
    $verified = Assert-InnoSetupCompiler -Directory (Split-Path $compiler -Parent) -Pin $pin
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $verified '/Qp' $ScriptPath 2>&1
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousErrorAction }
    Write-Utf8 $LogPath (($output | ForEach-Object { "$_" }) -join "`r`n")
    if ($exitCode -ne 0) { throw "Native fixture compiler failed ($exitCode); see $LogPath" }
}

# The include is copied byte-for-byte, with before/after hashes recorded below.
$includedPolicy = Join-Path $nativeRoot 'production-policy.iss'
Copy-Item -LiteralPath $policySource -Destination $includedPolicy
$nativeScript = @'
[Setup]
AppName=LegendCTL No-Install Policy Fixture
AppVersion=1
DefaultDirName={tmp}\LegendCTL-Never-Installed
PrivilegesRequired=lowest
CreateAppDir=no
Uninstallable=no
CreateUninstallRegKey=no
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=native-policy-fixture
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=none

[Code]
#include "production-policy.iss"

function InitializeSetup: Boolean;
var
  Lines: TArrayOfString;
  I, Separator: Integer;
  Row, Id, Selected, Managed, Expected, Actual, ResultsPath: String;
  Allowed: Boolean;
begin
  Result := False; { This fixture never reaches installation. }
  ResultsPath := ExpandConstant('{param:RESULTS}');
  if not LoadStringsFromFile(ExpandConstant('{param:CASES}'), Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do begin
    Row := Lines[I];
    Separator := Pos('|', Row);
    Id := Copy(Row, 1, Separator - 1);
    Delete(Row, 1, Separator);
    Separator := Pos('|', Row);
    Selected := Copy(Row, 1, Separator - 1);
    Delete(Row, 1, Separator);
    Separator := Pos('|', Row);
    Managed := Copy(Row, 1, Separator - 1);
    Delete(Row, 1, Separator);
    Expected := Row;
    Allowed := LegendInstallDirectoryAllowed(Selected, Managed);
    Actual := 'reject';
    if Allowed then Actual := 'allow';
    if not SaveStringToFile(ResultsPath, Id + '|' + Expected + '|' + Actual + #13#10, True) then
      Exit;
  end;
  SaveStringToFile(ResultsPath, 'COMPLETE' + #13#10, True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Any invocation means InitializeSetup no longer prevents installation. }
  SaveStringToFile(ExpandConstant('{param:FORBIDDEN}'), 'UNEXPECTED_INSTALL_STEP', False);
end;
'@
$nativeScriptPath = Join-Path $nativeRoot 'native-policy-fixture.iss'
Write-Utf8 $nativeScriptPath $nativeScript
Compile-Fixture $nativeScriptPath (Join-Path $nativeRoot 'compile.log')

$summary = [ordered]@{
    schema = 1
    backend = 'inno-pascal-script'
    policy_sha256 = Get-Sha256 $policySource
    included_policy_sha256 = Get-Sha256 $includedPolicy
    installer_sha256 = Get-Sha256 $installerSource
    harness_sha256 = Get-Sha256 $PSCommandPath
    compiler_sha256 = Get-Sha256 $compiler
    compiler_version = $pin.version
    fixture_sha256 = Get-Sha256 (Join-Path $nativeRoot 'native-policy-fixture.exe')
    native_status = 'NOT_RUN'
    vm_status = 'NOT_RUN'
    registry_behavior = 'NOT_TESTED: native fixture does not call registration helpers'
    cases = @()
}
if ($summary.policy_sha256 -ne $summary.included_policy_sha256) { throw 'Production include drifted.' }

if ($RunNativePolicy) {
    $fsRoot = Join-Path $nativeRoot 'filesystem'
    $ordinary = Join-Path $fsRoot 'ordinary'
    $outside = Join-Path $fsRoot 'outside'
    $descendantRoot = Join-Path $fsRoot 'descendant-root'
    $null = New-Item -ItemType Directory -Path $ordinary, $outside, $descendantRoot -Force
    $markerPath = Join-Path $outside 'untouched-marker.txt'
    Write-Utf8 $markerPath 'MUST REMAIN UNCHANGED'
    $markerBefore = Get-Sha256 $markerPath
    $fileCollision = Join-Path $fsRoot 'file-collision'
    Write-Utf8 $fileCollision 'MUST NOT BECOME A DIRECTORY'
    $fileBefore = Get-Sha256 $fileCollision
    $junction = Join-Path $fsRoot 'junction'
    $null = New-Item -ItemType Junction -Path $junction -Target $outside
    $null = New-Item -ItemType Junction -Path (Join-Path $descendantRoot '_internal') -Target $outside
    $missingLeaf = Join-Path $fsRoot 'missing-leaf'
    $missingParentLeaf = Join-Path $fsRoot 'missing-parent/app'
    $driveRoot = [IO.Path]::GetPathRoot($fsRoot)
    $cases = @(
        @('existing-directory', $ordinary, $ordinary, 'allow'),
        @('missing-leaf', $missingLeaf, $missingLeaf, 'allow'),
        @('case-insensitive', $ordinary.ToUpperInvariant(), $ordinary, 'allow'),
        @('trailing-separator', ($ordinary + '\'), $ordinary, 'allow'),
        @('drive-root', $driveRoot, $driveRoot, 'allow'),
        @('different-destination', $outside, $ordinary, 'reject'),
        @('sibling-prefix', ($ordinary + '-spoof'), $ordinary, 'reject'),
        @('app-subdirectory', ($ordinary + '\child'), $ordinary, 'reject'),
        @('dot-segment-escape', ($ordinary + '\..\outside'), $ordinary, 'reject'),
        @('dot-segment-safe-alias', ($ordinary + '\..\ordinary'), $ordinary, 'reject'),
        @('forward-slashes', $ordinary.Replace('\', '/'), $ordinary, 'reject'),
        @('device-path', ('\\?\' + $ordinary), $ordinary, 'reject'),
        @('unc-destination', '\\invalid.example\share\app', $ordinary, 'reject'),
        @('empty-destination', '', $ordinary, 'reject'),
        @('missing-ancestor', $missingParentLeaf, $missingParentLeaf, 'reject'),
        @('file-collision', $fileCollision, $fileCollision, 'reject'),
        @('file-ancestor', ($fileCollision + '\child'), ($fileCollision + '\child'), 'reject'),
        @('junction-root', $junction, $junction, 'reject'),
        @('junction-ancestor', ($junction + '\app'), ($junction + '\app'), 'reject'),
        @('invalid-attribute-name', ($fsRoot + '\bad?name'), ($fsRoot + '\bad?name'), 'reject'),
        # Explicit residual: helper checks the app root/ancestors, not descendants.
        @('residual-descendant-junction', $descendantRoot, $descendantRoot, 'allow')
    )
    $casePath = Join-Path $nativeRoot 'cases.txt'
    $resultPath = Join-Path $nativeRoot 'results.txt'
    $forbiddenPath = Join-Path $nativeRoot 'unexpected-install-step.txt'
    Write-Utf8 $casePath (($cases | ForEach-Object { $_ -join '|' }) -join "`r`n")
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
        ('/LOG="' + (Join-Path $nativeRoot 'native.log') + '"'),
        ('/CASES="' + $casePath + '"'), ('/RESULTS="' + $resultPath + '"'),
        ('/FORBIDDEN="' + $forbiddenPath + '"'))
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    try {
        $env:TEMP = $tempRoot
        $env:TMP = $tempRoot
        $process = Start-Process -FilePath (Join-Path $nativeRoot 'native-policy-fixture.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
        if (-not $process.WaitForExit(60000)) {
            $process.Kill()
            throw 'Native no-install fixture timed out; verification failed.'
        }
        $summary.native_exit_code = $process.ExitCode
    } finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }
    if (Test-Path -LiteralPath $forbiddenPath) { throw 'Fixture reached an installation step.' }
    $rows = @(Get-Content -LiteralPath $resultPath)
    if ($process.ExitCode -ne 1 -or $rows[-1] -ne 'COMPLETE' -or $rows.Count -ne ($cases.Count + 1)) {
        throw 'Native fixture did not complete its expected abort-before-install lifecycle.'
    }
    for ($i = 0; $i -lt $cases.Count; $i++) {
        $fields = $rows[$i].Split('|')
        if ($fields.Count -ne 3 -or $fields[0] -cne $cases[$i][0] -or $fields[1] -cne $cases[$i][3]) {
            throw 'Native result identity/expectation mismatch.'
        }
        $summary.cases += [ordered]@{ id = $fields[0]; expected = $fields[1]; actual = $fields[2] }
    }
    if ((Get-Sha256 $markerPath) -ne $markerBefore -or (Get-Sha256 $fileCollision) -ne $fileBefore -or
        (Test-Path -LiteralPath $missingLeaf) -or (Test-Path -LiteralPath $missingParentLeaf)) {
        throw 'Native helper modified its filesystem inputs.'
    }
    $summary.native_status = 'PASS'
    if (@($summary.cases | Where-Object { $_.expected -cne $_.actual }).Count) { $summary.native_status = 'FAIL' }
}

if ($BuildVmFixtures) {
    $vmRoot = Join-Path $runRoot 'vm-only'
    $null = New-Item -ItemType Directory -Path $vmRoot -Force
    $summary.vm_fixtures = @()
    $originalVersion = $env:ZDUL_VERSION
    try {
        foreach ($item in @(@('base', '0.0.1'), @('candidate', '0.0.2'), @('upgrade', '0.0.3'))) {
            $name = $item[0]
            $version = $item[1]
            $fixtureRoot = Join-Path $vmRoot $name
            $scriptRoot = Join-Path $fixtureRoot 'tools/installer'
            $payloadRoot = Join-Path $fixtureRoot ("dist/ZDUltimateLegend-v$version")
            $null = New-Item -ItemType Directory -Path $scriptRoot, $payloadRoot -Force
            $targetScript = Join-Path $scriptRoot 'inno_setup_zd_wrapper.iss'
            if ($name -eq 'base') {
                # Preserve the git blob bytes, not PowerShell's decoded line output.
                $start = New-Object Diagnostics.ProcessStartInfo
                $start.FileName = 'git'
                $start.Arguments = "-C `"$repoRoot`" cat-file blob ${BaselineRef}:tools/installer/inno_setup_zd_wrapper.iss"
                $start.UseShellExecute = $false
                $start.CreateNoWindow = $true
                $start.RedirectStandardOutput = $true
                $gitProcess = [Diagnostics.Process]::Start($start)
                $stream = [IO.File]::Create($targetScript)
                try { $gitProcess.StandardOutput.BaseStream.CopyTo($stream) } finally { $stream.Dispose() }
                $gitProcess.WaitForExit()
                if ($gitProcess.ExitCode -ne 0) { throw 'Could not read reviewed baseline installer blob.' }
            } else {
                Copy-Item -LiteralPath $installerSource -Destination $targetScript
                Copy-Item -LiteralPath $policySource -Destination (Join-Path $scriptRoot 'install_directory_policy.iss')
            }
            Write-Utf8 (Join-Path $payloadRoot 'ZD Ultimate Legend.exe') "NOT AN EXECUTABLE. Marker payload $name $version."
            Write-Utf8 (Join-Path $payloadRoot 'fixture-marker.txt') "LegendCTL installer fixture $name $version"
            $env:ZDUL_VERSION = $version
            Compile-Fixture $targetScript (Join-Path $fixtureRoot 'compile.log')
            $setupPath = Join-Path $fixtureRoot ("dist/ZDUltimateLegend-v$version-Setup.exe")
            $summary.vm_fixtures += [ordered]@{
                name = $name; version = $version
                baseline_ref = $(if ($name -eq 'base') { $BaselineRef } else { $null })
                source_sha256 = Get-Sha256 $targetScript
                setup_sha256 = Get-Sha256 $setupPath
                setup_relative_path = "vm-only/$name/dist/ZDUltimateLegend-v$version-Setup.exe"
            }
        }
    } finally { $env:ZDUL_VERSION = $originalVersion }
}
$summaryPath = Join-Path $runRoot 'summary.json'
Write-Utf8 $summaryPath ($summary | ConvertTo-Json -Depth 8)
Write-Output "Installer policy evidence: $summaryPath"
Write-Output "Native: $($summary.native_status); disposable VM lifecycle: $($summary.vm_status)"
if ($summary.native_status -eq 'FAIL') { throw 'One or more real Pascal policy cases failed; see summary.json.' }
