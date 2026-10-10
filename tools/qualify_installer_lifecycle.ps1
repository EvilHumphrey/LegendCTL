# Real installer qualification. NEVER run on a developer's installed machine.
# See docs/installer-lifecycle-qualification.md for the evidence boundary.
[CmdletBinding()]
param(
    [switch]$AllowDisposableRunner,
    [Parameter(Mandatory)][string]$CandidateSetup,
    [Parameter(Mandatory)][string]$CandidateSha256,
    [Parameter(Mandatory)][string]$CandidatePayloadDirectory,
    [Parameter(Mandatory)][string]$PreviousSetup,
    [Parameter(Mandatory)][string]$PreviousSha256,
    [Parameter(Mandatory)][string]$ExpectedVersion,
    [Parameter(Mandatory)][string]$ExpectedBuildCommit,
    [Parameter(Mandatory)][string]$EvidenceDirectory,
    [ValidateRange(30, 600)][int]$TimeoutSeconds = 180
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Require-Lifecycle([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Assert-DisposableRunner([bool]$OptIn) {
    Require-Lifecycle $OptIn 'Explicit -AllowDisposableRunner is required.'
    Require-Lifecycle ($env:GITHUB_ACTIONS -ceq 'true' -and $env:CI -ceq 'true' -and
        $env:RUNNER_ENVIRONMENT -ceq 'github-hosted' -and $env:RUNNER_OS -ceq 'Windows' -and
        $env:GITHUB_RUN_ID -match '^\d+$' -and -not [string]::IsNullOrWhiteSpace($env:GITHUB_JOB)) `
        'Only an explicitly opted-in GitHub-hosted Windows job may mutate the guest.'
    Require-Lifecycle ([Environment]::OSVersion.Platform -eq 'Win32NT' -and
        [Environment]::Is64BitProcess) 'Requires 64-bit Windows PowerShell.'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    Require-Lifecycle ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) `
        'An already elevated disposable runner token is required; never request UAC.'
    Require-Lifecycle (-not [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) 'RUNNER_TEMP is missing.'
}

function Assert-OrdinaryLocalPath([string]$Path, [switch]$AllowMissingLeaf) {
    Require-Lifecycle ($Path -match '^[A-Za-z]:\\' -and $Path.Substring(2) -notmatch ':' -and
        $Path -notmatch '(^|[\\/])\.\.?([\\/]|$)') 'Expected an absolute local path without aliases.'
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $cursor = $full
    $leaf = $true
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            Require-Lifecycle (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) `
                "Reparse path refused: $cursor"
            if (-not $leaf) { Require-Lifecycle $item.PSIsContainer "Non-directory ancestor: $cursor" }
        } else {
            Require-Lifecycle ($leaf -and $AllowMissingLeaf) "Missing path: $cursor"
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
        $leaf = $false
    }
    return $full
}

function Assert-OwnedChild([string]$Path, [string]$Root) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    Require-Lifecycle ($full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) `
        'Path is outside the exact task-owned root.'
    return $full
}

function Get-LifecycleHash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-LifecycleJson([string]$Path, $Value) {
    $json = ConvertTo-Json -InputObject $Value -Depth 30
    [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
}

function Get-LifecycleTree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $root = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $pending = [Collections.Generic.Queue[string]]::new()
    $rows = [Collections.Generic.List[object]]::new()
    $pending.Enqueue($root)
    while ($pending.Count) {
        $current = $pending.Dequeue()
        $item = Get-Item -LiteralPath $current -Force
        $link = ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        $kind = if ($link) { 'reparse' } elseif ($item.PSIsContainer) { 'directory' } else { 'file' }
        # Do not ask Get-Acl or hash operations to follow a reparse point.
        $row = [ordered]@{path=$current.Substring($root.Length); kind=$kind; attributes=[int]$item.Attributes}
        if ($link) { $row.target = @($item.Target) }
        else {
            $acl = Get-Acl -LiteralPath $current
            $row.owner = $acl.Owner
            $row.sddl = $acl.Sddl
            if ($item.PSIsContainer) {
                foreach ($child in @(Get-ChildItem -LiteralPath $current -Force | Sort-Object Name)) {
                    $pending.Enqueue($child.FullName)
                }
            } else { $row.size = $item.Length; $row.sha256 = Get-LifecycleHash $current }
        }
        $rows.Add($row)
    }
    return @($rows.ToArray())
}

function Get-LifecycleRegistry {
    $rows = [Collections.Generic.List[object]]::new()
    foreach ($hive in @('LocalMachine', 'CurrentUser')) {
        foreach ($view in @('Registry32', 'Registry64')) {
            $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
            $key = $root.OpenSubKey($script:UninstallKey)
            try {
                if ($key) {
                    $values = [ordered]@{}
                    foreach ($name in @($key.GetValueNames() | Sort-Object)) {
                        $values[$name] = @{kind="$($key.GetValueKind($name))";
                            value=$key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)}
                    }
                    $rows.Add(@{hive=$hive; view=$view; values=$values})
                }
            } finally { if ($key) { $key.Dispose() }; $root.Dispose() }
        }
    }
    return @($rows.ToArray())
}

function Get-LifecycleSnapshot {
    $trees = [ordered]@{}
    foreach ($path in $script:WatchedPaths) { $trees[$path] = @(Get-LifecycleTree $path) }
    return [ordered]@{registry=@(Get-LifecycleRegistry); trees=$trees}
}

function Assert-SameSnapshot($Before, $After) {
    Require-Lifecycle ((ConvertTo-Json -InputObject $Before -Depth 30 -Compress) -ceq
        (ConvertTo-Json -InputObject $After -Depth 30 -Compress)) 'Rejected setup mutated watched state.'
}

function Invoke-LifecycleProcess([string]$Id, [string]$Executable, [string]$Hash,
    [string[]]$Extra = @(), [switch]$Uninstall) {
    $null = Assert-OrdinaryLocalPath $Executable
    Require-Lifecycle ($Hash -match '^[0-9a-fA-F]{64}$') 'Invalid expected executable hash.'
    # Hold the exact image against replacement while verifying and executing it.
    $lock = [IO.File]::Open($Executable, 'Open', 'Read', 'Read')
    $log = Join-Path $script:Evidence ($Id + '.setup.log')
    $receipt = [ordered]@{id=$Id; executable=[IO.Path]::GetFileName($Executable); sha256=$Hash;
        started_utc=[DateTime]::UtcNow.ToString('o'); exit_code=$null; timed_out=$false; completed=$false}
    try {
        Require-Lifecycle ((Get-LifecycleHash $Executable) -ceq $Hash.ToLowerInvariant()) 'Executable hash changed before execution.'
        $arguments = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/LOG="' + $log + '"'))
        if (-not $Uninstall) {
            $arguments += @('/SP-','/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS','/RESTARTEXITCODE=3010')
        }
        $arguments += $Extra
        $receipt.arguments = $arguments
        $process = Start-Process -FilePath $Executable -ArgumentList $arguments -PassThru -WindowStyle Hidden
        $receipt.pid = $process.Id
        # The loaded uninstaller must remove its original image. Its process
        # now owns the verified executable; retaining a deny-delete handle
        # would itself break the lifecycle test.
        if ($Uninstall) { $lock.Dispose(); $lock = $null }
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $receipt.timed_out = $true
            # Do not kill an unknown child tree. Preserve evidence; the owning
            # disposable job is failed and its platform handles job teardown.
            throw "Installer timed out: $Id. Disposable job must not continue."
        }
        $process.Refresh()
        $receipt.exit_code = $process.ExitCode
        $receipt.completed = $true
        Require-Lifecycle (Test-Path -LiteralPath $log -PathType Leaf) 'Installer produced no required log.'
        $receipt.log_sha256 = Get-LifecycleHash $log
        return $receipt
    } finally {
        if ($lock) { $lock.Dispose() }
        $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
        Write-LifecycleJson (Join-Path $script:Evidence ($Id + '.process.json')) $receipt
    }
}

function Invoke-LifecycleCase([string]$Id, [scriptblock]$Action) {
    $before = Get-LifecycleSnapshot
    Write-LifecycleJson (Join-Path $script:Evidence ($Id + '.before.json')) $before
    $row = [ordered]@{id=$Id; status='RUNNING'; detail=$null}
    $script:Results.Add($row)
    try {
        & $Action $before
        Assert-NoSilentAppLaunch
        $row.status = 'PASS'
    } catch {
        $row.status = 'FAIL'
        $row.detail = $_.Exception.Message
        throw
    } finally {
        # Snapshot failures are themselves failures, never a null passing after.
        try { Write-LifecycleJson (Join-Path $script:Evidence ($Id + '.after.json')) (Get-LifecycleSnapshot) }
        catch { $row.status='FAIL'; $row.detail="After snapshot failed: $($_.Exception.Message)"; throw }
        finally { Write-LifecycleJson (Join-Path $script:Evidence 'cases.json') @($script:Results.ToArray()) }
    }
}

function Assert-NoSilentAppLaunch {
    $events = @(Get-Event -SourceIdentifier $script:ProcessEventId -ErrorAction SilentlyContinue)
    Write-LifecycleJson (Join-Path $script:Evidence 'app-process-events.json') @($events | ForEach-Object {
        @{process_id=$_.SourceEventArgs.NewEvent.ProcessID; name=$_.SourceEventArgs.NewEvent.ProcessName;
          observed=$_.TimeGenerated.ToUniversalTime().ToString('o')}
    })
    Require-Lifecycle ($events.Count -eq 0 -and @(Get-Process -Name 'ZD Ultimate Legend' -ErrorAction SilentlyContinue).Count -eq 0) `
        'The silent installer launched the application.'
}

function Assert-InstalledRegistration([string]$Version) {
    $rows = @(Get-LifecycleRegistry)
    Require-Lifecycle ($rows.Count -eq 1) 'Expected exactly one LegendCTL uninstall registration.'
    $reg = $rows[0]
    Require-Lifecycle ($reg.hive -ceq 'LocalMachine' -and $reg.view -ceq 'Registry64') 'Wrong uninstall registration hive/view.'
    foreach ($name in @('DisplayVersion','DisplayName','Publisher','InstallLocation','Inno Setup: App Path','UninstallString','QuietUninstallString')) {
        Require-Lifecycle ($reg.values.Contains($name) -and $reg.values[$name].kind -ceq 'String') "Missing/invalid uninstall value: $name"
    }
    Require-Lifecycle ($reg.values.DisplayVersion.value -ceq $Version) 'Installed version differs.'
    Require-Lifecycle ($reg.values.DisplayName.value.StartsWith('ZD Ultimate Legend Wrapper') -and
        $reg.values.Publisher.value -ceq 'EvilHumphrey') 'Installed product/publisher identity differs.'
    foreach ($name in @('InstallLocation','Inno Setup: App Path')) {
        Require-Lifecycle ($reg.values[$name].value.TrimEnd('\') -ieq $script:Managed) 'Noncanonical registered app path.'
    }
    $uninstaller = Join-Path $script:Managed 'unins000.exe'
    foreach ($name in @('UninstallString','QuietUninstallString')) {
        Require-Lifecycle ($reg.values[$name].value -match ('^"' + [regex]::Escape($uninstaller) + '"(?:\s|$)')) `
            'Registered uninstaller is outside the verified task installation.'
    }
    Require-Lifecycle (Test-Path -LiteralPath $uninstaller -PathType Leaf) 'Uninstaller missing.'
}

function Assert-InstalledPayload {
    foreach ($relative in $script:Payload.Keys) {
        $path = Join-Path $script:Managed $relative
        $null = Assert-OrdinaryLocalPath $path
        Require-Lifecycle ((Get-LifecycleHash $path) -ceq $script:Payload[$relative]) "Payload mismatch: $relative"
    }
    foreach ($row in @(Get-LifecycleTree $script:Managed)) {
        Require-Lifecycle ($row.kind -ne 'reparse') 'Installed payload contains a reparse point.'
        if ($row.kind -eq 'file') {
            $relative = $row.path.TrimStart('\')
            Require-Lifecycle ($script:Payload.Contains($relative) -or $relative -match '^unins\d{3}\.(exe|dat|msg)$') `
                "Unexpected/stale installed payload: $relative"
        }
    }
    $trust = Get-Content -LiteralPath (Join-Path $script:Managed '_internal\zd_app\trust_manifest.json') -Raw | ConvertFrom-Json
    Require-Lifecycle ($trust.version -ceq $ExpectedVersion -and $trust.build_commit -ceq $ExpectedBuildCommit -and
        $trust.build_commit_short -ceq $ExpectedBuildCommit) 'Installed source/version trust labels disagree.'
    $exe = Get-Item -LiteralPath (Join-Path $script:Managed 'ZD Ultimate Legend.exe')
    Require-Lifecycle ($exe.VersionInfo.ProductVersion -ceq "$ExpectedVersion.0") 'PE product version differs.'
}

function Assert-InstalledShortcuts {
    $shell = New-Object -ComObject WScript.Shell
    try {
        foreach ($entry in @(@('ZD Ultimate Legend Wrapper.lnk','ZD Ultimate Legend.exe'),
                             @('Uninstall ZD Ultimate Legend Wrapper.lnk','unins000.exe'))) {
            $path = Join-Path $script:CommonGroup $entry[0]
            Require-Lifecycle (Test-Path -LiteralPath $path -PathType Leaf) 'Expected Start Menu shortcut missing.'
            $shortcut = $shell.CreateShortcut($path)
            Require-Lifecycle ($shortcut.TargetPath -ieq (Join-Path $script:Managed $entry[1]) -and
                [string]::IsNullOrEmpty($shortcut.Arguments)) 'Incorrect shortcut target/arguments.'
        }
        foreach ($path in $script:DesktopLinks) {
            Require-Lifecycle (-not (Test-Path -LiteralPath $path)) 'Unexpected default desktop shortcut.'
        }
    } finally { $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
}

function Assert-DataPreserved {
    foreach ($path in $script:DataRoots) {
        $actual = @(Get-LifecycleTree $path)
        $expected = if ($path -ieq $script:DataRoot) { $script:DataSnapshot } else { @() }
        Require-Lifecycle ((ConvertTo-Json -InputObject $actual -Depth 20 -Compress) -ceq
            (ConvertTo-Json -InputObject $expected -Depth 20 -Compress)) "Installer changed per-user data: $path"
    }
}

function Invoke-ManagedUninstall([string]$Id) {
    $exe = Join-Path $script:Managed 'unins000.exe'
    $null = Assert-OrdinaryLocalPath $exe
    $result = Invoke-LifecycleProcess $Id $exe (Get-LifecycleHash $exe) -Uninstall
    Require-Lifecycle ($result.exit_code -eq 0) 'Uninstall failed or requires further investigation.'
    Require-Lifecycle (@(Get-LifecycleRegistry).Count -eq 0) 'Registration survived uninstall.'
    Require-Lifecycle (@(Get-LifecycleTree $script:Managed).Count -eq 0) 'Installer-owned files/directory survived uninstall.'
    foreach ($path in $script:ShortcutRoots) {
        Require-Lifecycle (@(Get-LifecycleTree $path).Count -eq 0) 'Shortcut survived uninstall.'
    }
    Assert-DataPreserved
}

function Invoke-RejectedSetup([string]$Id, [string[]]$Extra, [string]$ExpectedReason) {
    Invoke-LifecycleCase $Id {
        param($before)
        $result = Invoke-LifecycleProcess $Id $CandidateSetup $CandidateSha256 $Extra
        Require-Lifecycle ($result.exit_code -eq 7) 'Expected PrepareToInstall rejection (exit 7).'
        $log = Get-Content -LiteralPath (Join-Path $script:Evidence ($Id + '.setup.log')) -Raw
        Require-Lifecycle ($log.Contains($ExpectedReason)) 'Expected path-policy rejection was not reached.'
        Assert-SameSnapshot $before (Get-LifecycleSnapshot)
    }
}

function Initialize-AclProbe {
    # Disable admin-group membership and maximum privileges (Windows retains
    # SeChangeNotifyPrivilege), then perform
    # actual I/O while impersonating the restricted token. This is not UAC or a
    # second account test. Positive control prevents a universally denied token
    # from producing false protected-directory passes.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class LegendInstallerAclProbe {
    [StructLayout(LayoutKind.Sequential)] struct SID_AND_ATTRIBUTES { public IntPtr Sid; public uint Attributes; }
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr p, uint access, out IntPtr token);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool CreateRestrictedToken(IntPtr token, uint flags, uint count,
        [In] SID_AND_ATTRIBUTES[] disabled, uint pc, IntPtr privileges, uint sc, IntPtr restricted, out IntPtr result);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool ImpersonateLoggedOnUser(IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool RevertToSelf();
    public static string Check(string positiveControl, string[] protectedDirectories) {
        IntPtr original=IntPtr.Zero, limited=IntPtr.Zero;
        var sid=new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
        var bytes=new byte[sid.BinaryLength]; sid.GetBinaryForm(bytes,0);
        IntPtr mem=Marshal.AllocHGlobal(bytes.Length); Marshal.Copy(bytes,0,mem,bytes.Length);
        bool impersonating=false;
        try {
            if (!OpenProcessToken(GetCurrentProcess(), 0x0002|0x0008, out original)) throw new Win32Exception();
            var disabled=new[]{new SID_AND_ATTRIBUTES {Sid=mem,Attributes=0}};
            if (!CreateRestrictedToken(original,1,1,disabled,0,IntPtr.Zero,0,IntPtr.Zero,out limited)) throw new Win32Exception();
            if (!ImpersonateLoggedOnUser(limited)) throw new Win32Exception();
            impersonating=true;
            using (var identity=WindowsIdentity.GetCurrent()) {
                if (new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator)) throw new Exception("Probe still has administrator role.");
            }
            string positive=Path.Combine(positiveControl, "positive-"+Guid.NewGuid().ToString("N"));
            using (new FileStream(positive,FileMode.CreateNew,FileAccess.Write)) {}
            File.Delete(positive);
            foreach (string directory in protectedDirectories) {
                string path=Path.Combine(directory,"acl-probe-"+Guid.NewGuid().ToString("N"));
                bool denied=false;
                try { using (new FileStream(path,FileMode.CreateNew,FileAccess.Write)) {} }
                catch (UnauthorizedAccessException) { denied=true; }
                if (!denied) throw new Exception("Restricted non-admin token wrote protected directory: "+directory);
            }
            return "PASS: positive control writable; restricted non-admin token denied create in every installed directory";
        } finally {
            if (impersonating && !RevertToSelf()) Environment.FailFast("Cannot revert qualification impersonation");
            if(limited!=IntPtr.Zero) CloseHandle(limited);
            if(original!=IntPtr.Zero) CloseHandle(original);
            Marshal.FreeHGlobal(mem);
        }
    }
}
'@
}

function Assert-InstalledAcl([string]$Id) {
    $directories = @((Get-LifecycleTree $script:Managed) | Where-Object {$_.kind -eq 'directory'} |
        ForEach-Object { $script:Managed + $_.path })
    $result = [LegendInstallerAclProbe]::Check($script:Work, [string[]]$directories)
    Write-LifecycleJson (Join-Path $script:Evidence ($Id + '.acl.json')) @{
        status='PASS'; method='restricted-current-token-admin-SID-disabled-DISABLE_MAX_PRIVILEGE';
        evidence=$result; directories_checked=$directories.Count;
        limits='Not a distinct account/UAC test; probes creation, while captured SDDL supports further rights review.'
    }
}

function Invoke-InstallerLifecycle {
    # No mutation may precede the disposable-runner guard. Once admitted,
    # create only the fresh evidence directory so later preflight refusals can
    # be retained. App/registry/process mutations still require a clean guest.
    Assert-DisposableRunner ([bool]$AllowDisposableRunner)
    $tempRoot = Assert-OrdinaryLocalPath $env:RUNNER_TEMP
    $approvedEvidence = Assert-OrdinaryLocalPath $EvidenceDirectory -AllowMissingLeaf
    $null = Assert-OwnedChild $approvedEvidence $tempRoot
    Require-Lifecycle (-not (Test-Path -LiteralPath $approvedEvidence)) 'Evidence output must be new.'
    $null = New-Item -ItemType Directory -Path $approvedEvidence
    $script:Evidence = $approvedEvidence
    Require-Lifecycle ($ExpectedVersion -match '^\d+\.\d+\.\d+$' -and
        $ExpectedBuildCommit -match '^[0-9a-f]{40}$') 'Invalid expected release identity.'
    Require-Lifecycle ($CandidateSha256 -match '^[0-9a-fA-F]{64}$' -and
        $PreviousSha256 -match '^[0-9a-fA-F]{64}$') 'Invalid expected setup hash.'
    $CandidateSetup = Assert-OrdinaryLocalPath $CandidateSetup
    $PreviousSetup = Assert-OrdinaryLocalPath $PreviousSetup
    Require-Lifecycle ($CandidateSetup -ine $PreviousSetup -and $CandidateSha256 -ine $PreviousSha256) `
        'Upgrade requires distinct old and candidate installers.'
    Require-Lifecycle ((Get-LifecycleHash $CandidateSetup) -ceq $CandidateSha256.ToLowerInvariant() -and
        (Get-LifecycleHash $PreviousSetup) -ceq $PreviousSha256.ToLowerInvariant()) 'Setup input hash mismatch.'
    $payloadRoot = Assert-OrdinaryLocalPath $CandidatePayloadDirectory
    $script:Payload = [ordered]@{}
    foreach ($row in @(Get-LifecycleTree $payloadRoot)) {
        Require-Lifecycle ($row.kind -ne 'reparse') 'Reference payload contains reparse points.'
        if ($row.kind -eq 'file') { $script:Payload[$row.path.TrimStart('\')] = $row.sha256 }
    }
    Require-Lifecycle ($script:Payload.Contains('ZD Ultimate Legend.exe') -and
        $script:Payload.Contains('_internal\zd_app\trust_manifest.json')) 'Reference payload is incomplete.'
    $trust = Get-Content -LiteralPath (Join-Path $payloadRoot '_internal\zd_app\trust_manifest.json') -Raw | ConvertFrom-Json
    Require-Lifecycle ($trust.version -ceq $ExpectedVersion -and $trust.build_commit -ceq $ExpectedBuildCommit) `
        'Reference payload identity disagrees with requested candidate.'
    $script:Managed = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ZDUltimateLegend'
    $null = Assert-OrdinaryLocalPath $script:Managed -AllowMissingLeaf
    $script:UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{ZDUltimateLegend}_is1'
    $script:CommonGroup = Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'ZD Ultimate Legend Wrapper'
    $script:DesktopLinks = @('DesktopDirectory','CommonDesktopDirectory' | ForEach-Object {
        $root = [Environment]::GetFolderPath($_)
        if ($root) { Join-Path $root 'ZD Ultimate Legend Wrapper.lnk' }
    })
    $script:ShortcutRoots = @($script:CommonGroup) + $script:DesktopLinks
    $userPrograms = [Environment]::GetFolderPath('Programs')
    if ($userPrograms) { $script:ShortcutRoots += Join-Path $userPrograms 'ZD Ultimate Legend Wrapper' }
    $appdata = [Environment]::GetFolderPath('ApplicationData')
    Require-Lifecycle (-not [string]::IsNullOrWhiteSpace($appdata)) 'Invoking user roaming-data directory unavailable.'
    $null = Assert-OrdinaryLocalPath $appdata
    $script:DataRoot = Join-Path $appdata 'ZDUltimateLegend'
    $script:DataRoots = @(@($script:DataRoot) + @(Get-CimInstance Win32_UserProfile | ForEach-Object {
        if ($_.LocalPath) { Join-Path $_.LocalPath 'AppData\Roaming\ZDUltimateLegend' }
    }) | Sort-Object -Unique)
    $script:WatchedPaths = @($script:Managed) + $script:ShortcutRoots + $script:DataRoots
    Require-Lifecycle (@(Get-LifecycleRegistry).Count -eq 0) 'Existing LegendCTL registration: refusing runner.'
    foreach ($path in $script:WatchedPaths) {
        Require-Lifecycle (-not (Test-Path -LiteralPath $path)) 'Existing LegendCTL files, shortcuts or data: refusing runner.'
    }
    Require-Lifecycle (@(Get-Process -Name 'ZD Ultimate Legend' -ErrorAction SilentlyContinue).Count -eq 0) `
        'An existing LegendCTL process is running.'

    $script:Work = Join-Path $script:Evidence 'work'
    $null = New-Item -ItemType Directory -Path $script:Work
    $script:Results = [Collections.Generic.List[object]]::new()
    $script:ProcessEventId = 'LegendInstaller-' + [Guid]::NewGuid().ToString('N')
    $subscription = $null
    $summary = [ordered]@{schema=1; status='RUNNING'; scope='Real installer lifecycle on disposable GitHub Windows runner';
        source_commit=$ExpectedBuildCommit; candidate_version=$ExpectedVersion; previous_version='2.6.2';
        candidate_sha256=$CandidateSha256; previous_sha256=$PreviousSha256; os=[Environment]::OSVersion.VersionString;
        upgrade_kind=$(if ([version]$ExpectedVersion -gt [version]'2.6.2') {'higher-version'} else {'same-or-lower-version-replacement'});
        runner_image=$env:ImageOS; runner_image_version=$env:ImageVersion; github_run_id=$env:GITHUB_RUN_ID;
        harness_sha256=(Get-LifecycleHash $PSCommandPath); cases=@();
        limitations=@('Not Windows 10/11 or ARM64 qualification','No interactive UAC/SmartScreen/postinstall launch or hardware test',
        'Restricted-token ACL probe is not a second user account','No app launch/data migration or reboot/in-use qualification')}
    try {
        Write-LifecycleJson (Join-Path $script:Evidence 'initial-state.json') (Get-LifecycleSnapshot)
        Write-LifecycleJson (Join-Path $script:Evidence 'payload-reference.json') $script:Payload
        Initialize-AclProbe
        $subscription = Register-CimIndicationEvent -Query "SELECT * FROM Win32_ProcessStartTrace WHERE ProcessName = 'ZD Ultimate Legend.exe'" `
            -SourceIdentifier $script:ProcessEventId
        # All sentinels are newly created after the clean-machine check. No
        # recursive cleanup is used: evidence and failed state are retained.
        $null = New-Item -ItemType Directory -Path (Join-Path $script:DataRoot 'wrapper_profiles') -Force
        [IO.File]::WriteAllText((Join-Path $script:DataRoot 'settings.json'), '{"language":"ko","qualification_sentinel":"preserve"}')
        [IO.File]::WriteAllText((Join-Path $script:DataRoot 'wrapper_profiles\qualification.json'), '{"qualification_sentinel":"profile bytes preserved; not a runtime fixture"}')
        $script:DataSnapshot = @(Get-LifecycleTree $script:DataRoot)
        $writable = Join-Path $script:Work 'writable-install'
        $outside = Join-Path $script:Work 'outside'
        $null = New-Item -ItemType Directory -Path $outside
        [IO.File]::WriteAllText((Join-Path $outside 'sentinel.txt'), 'DO NOT CHANGE')
        $script:WatchedPaths += @($writable, $outside)
        $inf = Join-Path $script:Work 'hostile.inf'
        [IO.File]::WriteAllText($inf, "[Setup]`r`nDir=$writable`r`n")
        Invoke-RejectedSetup 'reject-writable-dir' @(('/DIR="' + $writable + '"')) 'LEGENDCTL_INSTALL_PATH_REJECTED'
        Invoke-RejectedSetup 'reject-hostile-inf' @(('/LOADINF="' + $inf + '"')) 'LEGENDCTL_INSTALL_PATH_REJECTED'
        $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine','Registry64')
        $key = $root.CreateSubKey($script:UninstallKey)
        try {
            $key.SetValue('InstallLocation',$writable,[Microsoft.Win32.RegistryValueKind]::String)
            $key.SetValue('Inno Setup: App Path',$writable,[Microsoft.Win32.RegistryValueKind]::String)
        } finally { $key.Dispose(); $root.Dispose() }
        Invoke-RejectedSetup 'reject-previous-registration' @() 'LEGENDCTL_PREVIOUS_INSTALL_PATH_REJECTED'
        # Only this exact key was absent at intake and created immediately above.
        $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine','Registry64')
        try { $root.DeleteSubKeyTree($script:UninstallKey, $false) } finally { $root.Dispose() }
        $null = New-Item -ItemType Junction -Path $script:Managed -Target $outside
        Invoke-RejectedSetup 'reject-root-junction' @() 'LEGENDCTL_INSTALL_PATH_REDIRECTED_OR_NOT_DIRECTORY'
        $item = Get-Item -LiteralPath $script:Managed -Force
        Require-Lifecycle (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -and
            @($item.Target).Count -eq 1 -and @($item.Target)[0] -ieq $outside) 'Task junction identity changed; refusing removal.'
        [IO.Directory]::Delete($script:Managed, $false)

        Invoke-LifecycleCase 'candidate-clean-install' {
            $result = Invoke-LifecycleProcess 'candidate-clean-install' $CandidateSetup $CandidateSha256
            Require-Lifecycle ($result.exit_code -eq 0) 'Clean install failed or requested reboot.'
            Assert-InstalledRegistration $ExpectedVersion
            Assert-InstalledPayload
            Assert-InstalledShortcuts
            Assert-InstalledAcl 'candidate-clean-install'
            Assert-DataPreserved
        }
        Invoke-LifecycleCase 'candidate-clean-uninstall' { Invoke-ManagedUninstall 'candidate-clean-uninstall' }
        Invoke-LifecycleCase 'previous-release-install' {
            $result = Invoke-LifecycleProcess 'previous-release-install' $PreviousSetup $PreviousSha256
            Require-Lifecycle ($result.exit_code -eq 0) 'Previous release installation failed.'
            Assert-InstalledRegistration '2.6.2'
            Assert-InstalledShortcuts
            Assert-DataPreserved
        }
        Invoke-LifecycleCase 'candidate-upgrade' {
            $result = Invoke-LifecycleProcess 'candidate-upgrade' $CandidateSetup $CandidateSha256
            Require-Lifecycle ($result.exit_code -eq 0) 'Upgrade failed or requested reboot.'
            Assert-InstalledRegistration $ExpectedVersion
            Assert-InstalledPayload
            Assert-InstalledShortcuts
            Assert-InstalledAcl 'candidate-upgrade'
            Assert-DataPreserved
        }
        Invoke-LifecycleCase 'candidate-upgrade-uninstall' { Invoke-ManagedUninstall 'candidate-upgrade-uninstall' }
        # Give the WMI event delivery queue a bounded drain before final verdict.
        Start-Sleep -Milliseconds 1000
        Assert-NoSilentAppLaunch
        Require-Lifecycle ($script:Results.Count -eq 9 -and
            @($script:Results | Where-Object {$_.status -cne 'PASS'}).Count -eq 0) 'Lifecycle matrix is incomplete.'
        $summary.status = 'PASS'
    } catch {
        $summary.status='FAIL'
        $summary.error=$_.Exception.Message
        throw
    } finally {
        $summary.cases=@($script:Results.ToArray())
        $summary.finished_utc=[DateTime]::UtcNow.ToString('o')
        Write-LifecycleJson (Join-Path $script:Evidence 'summary.json') $summary
        $hashes = [ordered]@{}
        foreach ($file in @(Get-ChildItem -LiteralPath $script:Evidence -File | Sort-Object Name)) {
            $hashes[$file.Name] = Get-LifecycleHash $file.FullName
        }
        Write-LifecycleJson (Join-Path $script:Evidence 'evidence-sha256.json') $hashes
        if ($subscription) {
            Unregister-Event -SourceIdentifier $script:ProcessEventId -ErrorAction SilentlyContinue
            Get-Event -SourceIdentifier $script:ProcessEventId -ErrorAction SilentlyContinue | Remove-Event -ErrorAction SilentlyContinue
        }
    }
}

function Start-InstallerLifecycle {
    $script:Evidence = $null
    try { Invoke-InstallerLifecycle }
    catch {
        # Never write on an unapproved host or to pre-existing/invalid output.
        # Once evidence admission succeeded, retain initial guard failures too.
        if ($script:Evidence) {
            $summaryPath = Join-Path $script:Evidence 'summary.json'
            if (-not (Test-Path -LiteralPath $summaryPath)) {
                Write-LifecycleJson $summaryPath @{
                    schema=1; status='BLOCKED'; phase='preflight'; error=$_.Exception.Message;
                    cases=@(); source_commit=$ExpectedBuildCommit; candidate_version=$ExpectedVersion;
                    candidate_sha256=$CandidateSha256; previous_sha256=$PreviousSha256;
                    finished_utc=[DateTime]::UtcNow.ToString('o')
                }
            }
        }
        throw
    }
}

Start-InstallerLifecycle
