# Reviewed Inno Setup toolchain

Release builds use exactly Inno Setup **6.7.1**. A package version, registry value,
compiler banner, or executable version resource is not proof of compiler bytes.
`tools/inno_setup.lock.json` is the reviewed SHA-256 allowlist. Both CI and local
`tools/build_release.ps1` use `tools/inno_setup.ps1` to enforce it without first
executing a compiler to ask its version.

## Acquisition and use

On disposable GitHub Actions runners, `tools/setup_inno_setup_ci.ps1` downloads
the exact official installer URL in the lock. It requires the expected SHA-256
and a **Valid** Authenticode signature with the locked publisher subject before
starting the installer. The installer is kept open without write/delete sharing
until it exits. Its portable mode prepares `.release-tools/inno-setup-6.7.1`
without uninstall registration, file associations, or shortcuts. Preparation
refuses to overlay an existing compiler directory, then checks the installed
compiler closure. This script refuses ordinary non-Actions invocation.

The normal local command remains:

```powershell
& tools\build_release.ps1
```

It never downloads or installs Inno Setup. It checks the private compiler folder
first, followed by the standard `ProgramFiles(x86)` and `ProgramFiles` Inno Setup
6 directories. A missing compiler preserves the existing ZIP-only build path;
the release workflow separately requires exactly one installer. A **present but
unapproved or incomplete** compiler is a hard error, without trying another
candidate. An installation of a newer version does not satisfy this lock.

The selected compiler is verified before packaging and again immediately before
the compile call. The lock covers 39 files: ISCC, its compiler/preprocessor DLLs,
compression DLLs/helper executables, setup/loader stubs, the shipped signature
sidecars, default messages and preprocessor builtins. Root-level IDE/signature
tool binaries are pinned too. Reparse points in a file or any parent path are
rejected, as are UNC/device paths and unexpected root executable/DLL/redirection
files. The ordinary uninstaller is not executed and is excluded from the lock.
This protects downloaded/stored toolchain inputs; it does not establish a trust
boundary against an administrator or a process that can concurrently rewrite the
repository, its scripts, the OS, or the reviewed manifest.

To verify an existing installation without running it:

```powershell
. tools\inno_setup.ps1
$pin = Read-InnoSetupPin
Assert-InnoSetupCompiler -Directory (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6') -Pin $pin
```

Direct manual `ISCC.exe` commands bypass this release gate. They are useful for
development only; publishable output must use the verified build path.

## Pin provenance and refresh

The 6.7.1 installer SHA-256 is
`4d11e8050b6185e0d49bd9e8cc661a7a59f44959a621d31d11033124c4e8a7b0`
(10,619,024 bytes), published by the
[immutable official release](https://github.com/jrsoftware/issrc/releases/tag/is-6_7_1).
The [official detached signature](https://files.jrsoftware.org/is/6/innosetup-6.7.1.exe.issig)
records the same digest and size. A downloaded copy was independently hashed and
had a valid Authenticode signature from Pyrsys B.V. The vendor documents the
[verification methods and publisher](https://jrsoftware.org/isdl-verify.php).

The initial file allowlist was seeded from an existing 6.7.1 installation:
the ISCC/IDE/signature-tool executables had valid vendor Authenticode signatures;
all 17 binary/stub signature pairs passed ISSigTool verification using the
vendor's [def01](https://github.com/jrsoftware/issrc/blob/is-6_7_1/def01.ispublickey)
and [def02](https://github.com/jrsoftware/issrc/blob/is-6_7_1/def02.ispublickey)
keys. ISSigTool's own Authenticode signature was checked before it was run.
`Default.isl` and `ISPPBuiltins.iss` matched the tagged official source bytes
exactly. These checks authenticate the seed assets; they are not a claim that
the installer was executed and independently extracted on the verification host.
The CI preparation path must also pass its post-install comparison.

For an update, review the vendor release and signature first, authenticate the
installer before executing it in a disposable environment, then compare and
review every compiler input and its hash. Update the lock and required-file set
together when upstream adds/removes assets. Review the upstream compiler's loaded
DLLs, helper executables, setup stubs, includes and messages, including any newly
referenced language files. Never generate trusted hashes from an unverified
download or a version banner. Run the focused tests, full suite, a CI rehearsal,
and exact-candidate installer qualification before publishing.

The focused regression command is:

```powershell
python -m unittest discover -s tests -p test_inno_setup_toolchain.py -v
```

It runs the real verifier under Windows PowerShell and PowerShell 7 when
available. Fixture executables are inert text and never run. Acquisition tests
replace only external download/signature/process APIs and test both rejection
and successful control paths. A green fixture test is not a substitute for an
actual CI toolchain preparation/build.
