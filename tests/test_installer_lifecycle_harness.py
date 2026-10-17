"""Fail-closed harness tests; no real installer, registry writes or UI launch.

PowerShell functions are extracted through its parser. The entrypoint is never
evaluated by mock tests. The only full-script tests exercise its earliest opt-in
guards with the CI environment removed. Temporary test files are synthetic.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/qualify_installer_lifecycle.ps1"
SHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(os.name == "nt" and SHELL, "Windows PowerShell harness tests")
class InstallerLifecycleHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legendctl-harness-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_ps(self, body: str, *, functions: bool = True):
        prefix = """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$source = $env:LEGEND_TEST_SOURCE
$testRoot = $env:LEGEND_TEST_ROOT
"""
        if functions:
            prefix += """
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count) { throw ($errors | Out-String) }
foreach($statement in $ast.EndBlock.Statements) {
    if($statement -is [Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
function New-SyntheticUninstallLog([string]$Original) {
    $messages=@('Log opened. (Time zone: UTC+00:00)', 'Setup version: Inno Setup version 6.7.1',
        ('Original Uninstall EXE: '+$Original), 'Starting the uninstallation process.',
        'Uninstallation process succeeded.', 'Removed all? Yes', 'Need to restart Windows? No', 'Log closed.')
    return (($messages | ForEach-Object {'2026-01-01 00:00:00.000   '+$_}) -join "`r`n")+"`r`n"
}
"""
        test_script = self.root / "case.ps1"
        test_script.write_text(prefix + body, encoding="utf-8")
        env = os.environ.copy()
        for name in ("GITHUB_ACTIONS", "CI", "RUNNER_ENVIRONMENT", "RUNNER_OS", "GITHUB_RUN_ID", "GITHUB_JOB"):
            env.pop(name, None)
        env.update(LEGEND_TEST_SOURCE=str(SCRIPT), LEGEND_TEST_ROOT=str(self.root))
        return subprocess.run(
            [SHELL, "-NoProfile", "-NonInteractive", "-File", str(test_script)],
            capture_output=True, text=True, timeout=45, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_entrypoint_requires_opt_in_before_any_output_or_launch(self):
        result = self.run_ps("""
$output = Join-Path $testRoot 'must-not-exist'
try {
    & $source -CandidateSetup 'missing' -CandidateSha256 ('a'*64) -CandidatePayloadDirectory 'missing' `
      -PreviousSetup 'missing' -PreviousSha256 ('b'*64) -ExpectedVersion '2.6.3' `
      -ExpectedBuildCommit ('c'*40) -EvidenceDirectory $output
    throw 'entrypoint unexpectedly returned'
} catch {
    if(-not $_.Exception.Message.Contains('Explicit -AllowDisposableRunner')) { throw }
}
if(Test-Path -LiteralPath $output) { throw 'guard wrote evidence before admission' }
""", functions=False)
        self.assert_success(result)

    def test_opt_in_alone_cannot_enable_host_mutations(self):
        result = self.run_ps("""
$output = Join-Path $testRoot 'must-not-exist'
try {
    & $source -AllowDisposableRunner -CandidateSetup 'missing' -CandidateSha256 ('a'*64) `
      -CandidatePayloadDirectory 'missing' -PreviousSetup 'missing' -PreviousSha256 ('b'*64) `
      -ExpectedVersion '2.6.3' -ExpectedBuildCommit ('c'*40) -EvidenceDirectory $output
    throw 'entrypoint unexpectedly returned'
} catch {
    if(-not $_.Exception.Message.Contains('GitHub-hosted Windows job')) { throw }
}
if(Test-Path -LiteralPath $output) { throw 'host guard wrote evidence' }
""", functions=False)
        self.assert_success(result)

    def test_owned_path_boundary_rejects_sibling_prefix_and_traversal(self):
        result = self.run_ps(r"""
$owned=Join-Path $testRoot 'owned'
$null=Assert-OwnedChild (Join-Path $owned 'child') $owned
foreach($path in @($owned, $owned+'-sibling\file', $owned+'\..\escape')) {
    $rejected=$false
    try { $null=Assert-OwnedChild $path $owned } catch { $rejected=$true }
    if(-not $rejected) { throw "escaped ownership boundary: $path" }
}
""")
        self.assert_success(result)

    def test_tree_snapshots_preserve_empty_directories_and_detect_changed_bytes(self):
        result = self.run_ps("""
$root=Join-Path $testRoot 'tree'
$null=New-Item -ItemType Directory -Path (Join-Path $root 'empty')
$file=Join-Path $root 'payload.txt'
[IO.File]::WriteAllText($file,'before')
$before=@(Get-LifecycleTree $root)
if(@($before | Where-Object {$_.kind -eq 'directory'}).Count -ne 2) { throw 'empty directory lost' }
[IO.File]::WriteAllText($file,'after')
$after=@(Get-LifecycleTree $root)
$rejected=$false
try { Assert-SameSnapshot $before $after } catch { $rejected=$true }
if(-not $rejected) { throw 'changed file was accepted as unchanged' }
Assert-SameSnapshot $after $after
""")
        self.assert_success(result)

    def test_data_preservation_accepts_empty_single_and_multiple_snapshot_rows(self):
        result = self.run_ps("""
$script:DataRoot=Join-Path $testRoot 'invoking-user'
$other=Join-Path $testRoot 'other-user'
$script:DataRoots=@($script:DataRoot,$other)
foreach($state in @('absent','empty','populated')) {
    if($state -eq 'empty') { $null=New-Item -ItemType Directory -Path $script:DataRoot }
    if($state -eq 'populated') {
        [IO.File]::WriteAllText((Join-Path $script:DataRoot 'settings.json'),'original settings')
        $null=New-Item -ItemType Directory -Path (Join-Path $script:DataRoot 'profiles')
        [IO.File]::WriteAllText((Join-Path $script:DataRoot 'profiles/one.json'),'original profile')
    }
    $script:DataSnapshot=@(Get-LifecycleTree $script:DataRoot)
    $expectedCount=@{absent=0;empty=1;populated=4}[$state]
    if($script:DataSnapshot.Count -ne $expectedCount) { throw 'fixture did not cover snapshot cardinality' }
    Assert-DataPreserved
}
""")
        self.assert_success(result)

    def test_data_preservation_rejects_changed_missing_and_unexpected_profile_data(self):
        result = self.run_ps("""
$script:DataRoot=Join-Path $testRoot 'invoking-user'
$other=Join-Path $testRoot 'other-user'
$script:DataRoots=@($script:DataRoot,$other)
$null=New-Item -ItemType Directory -Path $script:DataRoot
$file=Join-Path $script:DataRoot 'settings.json'
[IO.File]::WriteAllText($file,'original settings')
$script:DataSnapshot=@(Get-LifecycleTree $script:DataRoot)
foreach($case in @('changed','missing','unexpected')) {
    [IO.File]::WriteAllText($file,'original settings')
    if($case -eq 'changed') { [IO.File]::WriteAllText($file,'changed settings') }
    if($case -eq 'missing') { Remove-Item -LiteralPath $file }
    if($case -eq 'unexpected') {
        $null=New-Item -ItemType Directory -Path $other
        [IO.File]::WriteAllText((Join-Path $other 'settings.json'),'unexpected settings')
    }
    $rejected=$false
    try { Assert-DataPreserved } catch { $rejected=$true }
    if(-not $rejected) { throw "data mutation was accepted: $case" }
}
""")
        self.assert_success(result)

    def test_wrong_hash_cannot_reach_mock_launcher_and_retains_failure_receipt(self):
        result = self.run_ps("""
$script:Evidence=$testRoot
$TimeoutSeconds=30
$exe=Join-Path $testRoot 'not-an-executable.txt'
[IO.File]::WriteAllText($exe,'synthetic')
$script:launches=0
function Start-Process { $script:launches++; throw 'MUST NOT LAUNCH' }
$rejected=$false
try { Invoke-LifecycleProcess 'bad-hash' $exe ('a'*64) } catch { $rejected=$true }
if(-not $rejected -or $script:launches -ne 0) { throw 'hash did not guard launch' }
$receipt=Get-Content (Join-Path $testRoot 'bad-hash.process.json') -Raw | ConvertFrom-Json
if($receipt.completed -or $null -ne $receipt.exit_code) { throw 'false success receipt' }
""")
        self.assert_success(result)

    def test_mock_launch_is_hidden_bounded_and_restart_or_failure_not_rewritten_to_success(self):
        result = self.run_ps("""
$script:Evidence=$testRoot
$TimeoutSeconds=30
$exe=Join-Path $testRoot 'not-an-executable.txt'
[IO.File]::WriteAllText($exe,'synthetic')
function Start-Process {
    param($FilePath,$ArgumentList,[switch]$PassThru,$WindowStyle)
    if($WindowStyle -cne 'Hidden') { throw 'not hidden' }
    foreach($flag in @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS')) {
        if($flag -notin $ArgumentList) { throw "missing $flag" }
    }
    [IO.File]::WriteAllText((Join-Path $testRoot 'mock.setup.log'),'synthetic log')
    $p=[pscustomobject]@{Id=123; ExitCode=3010}
    $p | Add-Member ScriptMethod WaitForExit {param($ms) if($ms -le 0 -or $ms -gt 30000) {throw 'unbounded wait'}; return $true}
    $p | Add-Member ScriptMethod Refresh {}
    return $p
}
$result=Invoke-LifecycleProcess 'mock' $exe (Get-LifecycleHash $exe)
if($result.exit_code -ne 3010 -or -not $result.completed) { throw 'lost actual exit code' }
""")
        self.assert_success(result)

    def test_mock_timeout_retains_receipt_without_killing_other_processes(self):
        result = self.run_ps("""
$script:Evidence=$testRoot
$TimeoutSeconds=30
$exe=Join-Path $testRoot 'not-an-executable.txt'
[IO.File]::WriteAllText($exe,'synthetic')
function Start-Process {
    $p=[pscustomobject]@{Id=123; ExitCode=$null}
    $p | Add-Member ScriptMethod WaitForExit {param($ms) return $false}
    return $p
}
function Stop-Process { throw 'must not kill processes' }
$rejected=$false
try { Invoke-LifecycleProcess 'timeout' $exe (Get-LifecycleHash $exe) } catch { $rejected=$true }
if(-not $rejected) { throw 'timeout incorrectly passed' }
$r=Get-Content (Join-Path $testRoot 'timeout.process.json') -Raw | ConvertFrom-Json
if(-not $r.timed_out -or $r.completed -or $null -ne $r.exit_code) { throw 'bad timeout receipt' }
""")
        self.assert_success(result)

    def test_uninstall_launcher_exit_waits_for_writer_close_before_hashing_or_completion(self):
        result = self.run_ps("""
$script:Evidence=$testRoot; $TimeoutSeconds=30
$exe=Join-Path $testRoot 'synthetic-uninstaller.txt'
[IO.File]::WriteAllText($exe,'not executable')
$script:polls=0; $script:writer=$null
function Start-Process {
    $log=Join-Path $testRoot 'uninstall.setup.log'
    $script:writer=[IO.File]::Open($log,'Create','Write','Read')
    $bytes=[Text.Encoding]::UTF8.GetBytes((New-SyntheticUninstallLog $exe))
    $script:writer.Write($bytes,0,$bytes.Length); $script:writer.Flush()
    $p=[pscustomobject]@{Id=123; ExitCode=0}
    $p | Add-Member ScriptMethod WaitForExit {param($ms) return $true}
    $p | Add-Member ScriptMethod Refresh {}
    return $p
}
function Start-Sleep {
    param($Milliseconds)
    $script:polls++
    if($Milliseconds -le 0 -or $Milliseconds -gt 50) {throw 'invalid polling interval'}
    if($Receipt.completed -or $Receipt.launcher_exit_code -ne 0 -or $null -ne $Receipt.worker_exit_code) {
        throw 'launcher exit incorrectly promoted to worker completion'
    }
    $script:writer.Dispose(); $script:writer=$null
}
try {$r=Invoke-LifecycleProcess 'uninstall' $exe (Get-LifecycleHash $exe) -Uninstall}
finally {if($script:writer){$script:writer.Dispose()}}
if($script:polls -ne 1 -or $r.handoff_polls -ne 2 -or -not $r.completed -or $r.timed_out) {throw 'handoff not observed'}
if($r.worker_completion -cne 'inno-6.7.1-closed-log-success-no-restart' -or $null -ne $r.worker_exit_code) {throw 'invented child exit'}
if($r.log_sha256 -cne (Get-LifecycleHash (Join-Path $testRoot 'uninstall.setup.log'))) {throw 'completion hash differs'}
""")
        self.assert_success(result)

    def test_closed_log_accepts_pinned_success_and_rejects_other_outcomes_or_identity(self):
        result = self.run_ps(r"""
$exe=Join-Path $testRoot 'synthetic-uninstaller.txt'
$path=Join-Path $testRoot 'synthetic.log'
$valid=New-SyntheticUninstallLog $exe
[IO.File]::WriteAllText($path,$valid)
$r=Read-ClosedUninstallLog $path $exe
if(-not $r -or $r.log_sha256 -cne (Get-LifecycleHash $path)) {throw 'valid log rejected'}
foreach($case in @('wrong-version','wrong-owner','failed','restart','partial-removal','late-exception','untimestamped')) {
    $text=$valid
    if($case -eq 'wrong-version') {$text=$text.Replace('version 6.7.1','version 6.7.2')}
    if($case -eq 'wrong-owner') {$text=$text.Replace($exe,($exe+'-other'))}
    if($case -eq 'failed') {$text=$text.Replace('process succeeded.','process failed.')}
    if($case -eq 'restart') {$text=$text.Replace('restart Windows? No','restart Windows? Yes')}
    if($case -eq 'partial-removal') {$text=$text.Replace('Removed all? Yes','Removed all? No')}
    if($case -eq 'late-exception') {$text=$text.Replace('Log closed.',"Exception message:`r`n2026-01-01 00:00:00.000   Log closed.")}
    if($case -eq 'untimestamped') {$text=$text.Replace('2026-01-01 00:00:00.000   Removed all? Yes','Removed all? Yes')}
    [IO.File]::WriteAllText($path,$text)
    $rejected=$false
    try {$null=Read-ClosedUninstallLog $path $exe} catch {$rejected=$true}
    if(-not $rejected) {throw "bad completion evidence accepted: $case"}
}
[IO.File]::WriteAllText($path,($valid -replace '[^\r\n]*Log closed\.\r\n',''))
if($null -ne (Read-ClosedUninstallLog $path $exe)) {throw 'partial log accepted as complete'}
if($null -ne (Read-ClosedUninstallLog (Join-Path $testRoot 'absent.log') $exe)) {throw 'absent log accepted'}
""")
        self.assert_success(result)

    def test_uninstall_handoff_uses_remaining_overall_budget_and_preserves_unknown_worker_exit(self):
        result = self.run_ps("""
$script:Evidence=$testRoot; $TimeoutSeconds=1
$exe=Join-Path $testRoot 'synthetic-uninstaller.txt'
[IO.File]::WriteAllText($exe,'not executable')
function Start-Process {
    [IO.File]::WriteAllText((Join-Path $testRoot 'budget.setup.log'),'unfinished worker log')
    Start-Sleep -Milliseconds 900
    $p=[pscustomobject]@{Id=123; ExitCode=0}
    $p | Add-Member ScriptMethod WaitForExit {param($ms) if($ms -lt 0 -or $ms -gt 100) {throw 'launcher received a fresh budget'}; return $true}
    $p | Add-Member ScriptMethod Refresh {}
    return $p
}
$rejected=$false
try {$null=Invoke-LifecycleProcess 'budget' $exe (Get-LifecycleHash $exe) -Uninstall} catch {$rejected=$true}
if(-not $rejected){throw 'unfinished worker passed'}
$r=Get-Content (Join-Path $testRoot 'budget.process.json') -Raw | ConvertFrom-Json
if($r.completed -or -not $r.timed_out -or $r.launcher_exit_code -ne 0 -or $r.exit_code -ne 0 -or $null -ne $r.worker_exit_code) {
    throw 'handoff timeout receipt confuses launcher and worker'
}
if($r.worker_completion -cne 'timeout-waiting-for-closed-log' -or $r.handoff_polls -gt 2) {throw 'handoff restarted timeout budget'}
""")
        self.assert_success(result)

    def test_closed_failed_uninstall_never_sets_completed_even_with_launcher_exit_zero(self):
        result = self.run_ps("""
$script:Evidence=$testRoot; $TimeoutSeconds=30
$exe=Join-Path $testRoot 'synthetic-uninstaller.txt'
[IO.File]::WriteAllText($exe,'not executable')
function Start-Process {
    $text=(New-SyntheticUninstallLog $exe).Replace('process succeeded.','process failed.')
    [IO.File]::WriteAllText((Join-Path $testRoot 'failed.setup.log'),$text)
    $p=[pscustomobject]@{Id=123; ExitCode=0}
    $p | Add-Member ScriptMethod WaitForExit {param($ms) return $true}
    $p | Add-Member ScriptMethod Refresh {}
    return $p
}
$rejected=$false
try {$null=Invoke-LifecycleProcess 'failed' $exe (Get-LifecycleHash $exe) -Uninstall} catch {$rejected=$true}
if(-not $rejected) {throw 'worker failure passed'}
$r=Get-Content (Join-Path $testRoot 'failed.process.json') -Raw | ConvertFrom-Json
if($r.completed -or $r.launcher_exit_code -ne 0 -or $r.worker_completion -cne 'rejected-completion-evidence') {throw 'wrong failure receipt'}
""")
        self.assert_success(result)

    def test_locked_log_hash_failure_preserves_primary_error_and_partial_manifest(self):
        result = self.run_ps("""
$script:Evidence=$testRoot
$path=Join-Path $testRoot 'active.setup.log'
$writer=[IO.File]::Open($path,'Create','Write','None')
$summary=[ordered]@{status='FAIL';error='primary worker timeout'}
try {
    try {try {throw 'primary worker timeout'} finally {Complete-LifecycleEvidence $summary}}
    catch {if($_.Exception.Message -cne 'primary worker timeout') {throw 'primary failure masked'}}
    $r=Get-Content (Join-Path $testRoot 'summary.json') -Raw | ConvertFrom-Json
    if($r.evidence_hashes_complete -or $r.error -cne 'primary worker timeout') {throw 'incomplete evidence mislabeled'}
    $manifest=Get-Content (Join-Path $testRoot 'evidence-sha256.json') -Raw | ConvertFrom-Json
    if(-not $manifest.'summary.json' -or -not $r.evidence_hash_errors.'active.setup.log') {throw 'partial manifest missing'}
    $rejected=$false
    try {Complete-LifecycleEvidence ([ordered]@{status='PASS'})} catch {$rejected=$true}
    if(-not $rejected) {throw 'success with incomplete hashing accepted'}
} finally {$writer.Dispose()}
""")
        self.assert_success(result)

    def test_rejection_needs_exact_stage_reason_and_preservation(self):
        result = self.run_ps("""
$script:Evidence=$testRoot
$CandidateSetup='synthetic'; $CandidateSha256='synthetic'
function Invoke-LifecycleCase { param($Id,$Action); & $Action @{value='before'} }
function Invoke-LifecycleProcess { return @{exit_code=$script:fakeExit} }
function Get-LifecycleSnapshot { return @{value=$script:fakeState} }
$script:fakeExit=7; $script:fakeState='before'
[IO.File]::WriteAllText((Join-Path $testRoot 'reject.setup.log'),'EXPECTED_REASON')
Invoke-RejectedSetup 'reject' @() 'EXPECTED_REASON'
foreach($case in @('wrong-exit','wrong-reason','mutated-state')) {
    $script:fakeExit=7; $script:fakeState='before'
    [IO.File]::WriteAllText((Join-Path $testRoot 'reject.setup.log'),'EXPECTED_REASON')
    if($case -eq 'wrong-exit') { $script:fakeExit=1 }
    if($case -eq 'wrong-reason') { [IO.File]::WriteAllText((Join-Path $testRoot 'reject.setup.log'),'blocked before policy') }
    if($case -eq 'mutated-state') { $script:fakeState='after' }
    $rejected=$false
    try { Invoke-RejectedSetup 'reject' @() 'EXPECTED_REASON' } catch { $rejected=$true }
    if(-not $rejected) { throw "false rejection pass: $case" }
}
""")
        self.assert_success(result)

    def test_case_failure_preserves_both_snapshots_and_does_not_mark_pass(self):
        result = self.run_ps("""
$script:Evidence=$testRoot
$script:Results=[Collections.Generic.List[object]]::new()
function Get-LifecycleSnapshot { return @{synthetic='state'} }
function Assert-NoSilentAppLaunch { throw 'unexpected application launch' }
$rejected=$false
try { Invoke-LifecycleCase 'case' {} } catch { $rejected=$true }
if(-not $rejected) { throw 'failed assertion passed' }
foreach($name in @('case.before.json','case.after.json','cases.json')) {
    if(-not(Test-Path (Join-Path $testRoot $name))) { throw 'missing failure evidence' }
}
if($script:Results[0].status -cne 'FAIL') { throw 'false PASS' }
""")
        self.assert_success(result)

    def test_acl_probe_native_declarations_compile_without_executing_probe(self):
        self.assert_success(self.run_ps("Initialize-AclProbe\n"))

    def test_admitted_preflight_failure_is_serialized_before_rethrow(self):
        result = self.run_ps("""
$ExpectedBuildCommit='c'*40; $ExpectedVersion='2.6.3'; $CandidateSha256='a'*64; $PreviousSha256='b'*64
function Invoke-InstallerLifecycle {$script:Evidence=$testRoot; throw 'synthetic clean-machine refusal'}
$rejected=$false
try {Start-InstallerLifecycle} catch {$rejected=$true}
if(-not $rejected) {throw 'preflight failure swallowed'}
$r=Get-Content (Join-Path $testRoot 'summary.json') -Raw | ConvertFrom-Json
if($r.status -cne 'BLOCKED' -or $r.phase -cne 'preflight' -or $r.cases.Count -ne 0) {throw 'false preflight verdict'}
""")
        self.assert_success(result)

    def test_installed_payload_rejects_changed_bytes_stale_files_and_wrong_source(self):
        result = self.run_ps(r"""
$script:Managed=Join-Path $testRoot 'synthetic-install'
$script:Payload=[ordered]@{'ZD Ultimate Legend.exe'='expected'; '_internal\zd_app\trust_manifest.json'='trust'}
$ExpectedVersion='2.6.3'; $ExpectedBuildCommit='c'*40
$script:actualHash='expected'; $script:extra=$false; $script:actualCommit=$ExpectedBuildCommit
function Assert-OrdinaryLocalPath {}
function Get-LifecycleHash {param($Path); if($Path.EndsWith('.exe')) {return $script:actualHash}; return 'trust'}
function Get-LifecycleTree {
    $rows=@(@{kind='file';path='\ZD Ultimate Legend.exe'},@{kind='file';path='\_internal\zd_app\trust_manifest.json'})
    if($script:extra) {$rows+=@{kind='file';path='\_internal\old-vulnerable.dll'}}
    return $rows
}
function Get-Content {
    @{version='2.6.3';build_commit=$script:actualCommit;build_commit_short=$script:actualCommit} | ConvertTo-Json
}
function Get-Item { return @{VersionInfo=@{ProductVersion='2.6.3.0'}} }
Assert-InstalledPayload
foreach($case in @('changed-bytes','stale-file','wrong-source')) {
    $script:actualHash='expected'; $script:extra=$false; $script:actualCommit=$ExpectedBuildCommit
    if($case -eq 'changed-bytes') {$script:actualHash='changed'}
    if($case -eq 'stale-file') {$script:extra=$true}
    if($case -eq 'wrong-source') {$script:actualCommit='d'*40}
    $rejected=$false
    try { Assert-InstalledPayload } catch {$rejected=$true}
    if(-not $rejected) {throw "bad installed payload accepted: $case"}
}
""")
        self.assert_success(result)

    def test_registered_uninstaller_must_stay_in_owned_installation(self):
        result = self.run_ps(r"""
$script:Managed=Join-Path $testRoot 'synthetic-install'
$exe=Join-Path $script:Managed 'unins000.exe'
$script:reg=[ordered]@{}
foreach($pair in @(@('DisplayVersion','2.6.3'),@('DisplayName','ZD Ultimate Legend Wrapper'),
    @('Publisher','EvilHumphrey'),@('InstallLocation',$script:Managed),@('Inno Setup: App Path',$script:Managed),
    @('UninstallString',('"'+$exe+'"')),@('QuietUninstallString',('"'+$exe+'" /SILENT')))) {
    $script:reg[$pair[0]]=@{kind='String';value=$pair[1]}
}
function Get-LifecycleRegistry { return @{hive='LocalMachine';view='Registry64';values=$script:reg} }
function Test-Path {return $true}
Assert-InstalledRegistration '2.6.3'
$script:reg.QuietUninstallString.value='"C:\outside\unins000.exe" /SILENT'
$rejected=$false
try {Assert-InstalledRegistration '2.6.3'} catch {$rejected=$true}
if(-not $rejected) {throw 'external uninstaller accepted'}
""")
        self.assert_success(result)


if __name__ == "__main__":
    unittest.main()
