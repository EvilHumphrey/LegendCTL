# Code-signing policy

This document describes how LegendCTL release binaries are built and signed. It
is also a prerequisite for LegendCTL's application to the
[SignPath Foundation](https://signpath.org) free code-signing program for
open-source software.

## Summary

- Release executables and installers are (will be) **code-signed via the
  SignPath Foundation**, using a certificate issued to this project.
- We sign **only our own binaries, built from this repository's source.** No
  third-party, vendored, or externally supplied executable is ever signed.
- Signing is the **last** step of a repeatable, documented build from a tagged commit.

## Build & release process

1. **Source of truth.** All code lives in this public repository. Releases are
   cut from a specific tagged commit on the default branch.
2. **Build from source.** The release is produced by the project's own build
   script:
   ```powershell
   .\tools\build_release.ps1
   ```
   This packages the app with PyInstaller into `dist/…/ZD Ultimate Legend.exe`
   plus a distributable ZIP (and an installer `.exe` when Inno Setup is present).
3. **Verify the build.** The release smoke test (`.\tools\smoke_release.ps1`)
   and the unit suite are run before a build is considered releasable.
4. **Publish hashes.** Each release publishes `SHA256SUMS.txt` for every
   distributed artifact, so users can verify integrity independently of the
   signature.
5. **Sign.** The release artifacts are submitted to SignPath for signing as the
   final step. Only artifacts produced by step 2 from the tagged commit are
   submitted.

## Signing scope

We sign **only**:

- executables and installers built from this repository, for an official tagged
  release.

We do **not** sign:

- builds from forks or untrusted branches;
- third-party binaries or dependencies (each dependency keeps its own upstream
  signature and license — see [NOTICE](../NOTICE));
- anything a contributor sends pre-built. Pull requests contribute **source**,
  which is reviewed and then built by the maintainer — never a pre-compiled
  binary.

## Team & roles

LegendCTL is currently a **solo-maintainer** project. For SignPath's role model,
the single maintainer (copyright holder **EvilHumphrey**) holds all three roles:

| Role | Responsibility | Holder |
| --- | --- | --- |
| Author | Requests the signed build | solo maintainer |
| Reviewer | Reviews the change being signed | solo maintainer |
| Approver | Approves the signing request | solo maintainer |

If additional maintainers join, these roles will be separated (a different
person approving the signing than authoring it) and this document updated.

## Account security

- The maintainer's source-host account and the SignPath account both have
  **multi-factor authentication (MFA)** enabled.
- The signing certificate's private key is held by SignPath; the project never
  takes custody of the private key.
- Signing requests are approved **manually for each release** — signing is not
  automated to fire on every commit or every CI run.

## How users verify a signed release

Once signing is live, you can confirm a downloaded build is the signed release:

1. Right-click `ZD Ultimate Legend.exe` → **Properties → Digital Signatures**
   and confirm the signer is the LegendCTL project certificate.
2. Independently, check the downloaded artifact's hash against the published `SHA256SUMS.txt`:
   ```powershell
   Get-FileHash ".\ZDUltimateLegend-v<version>-windows.zip" -Algorithm SHA256
   ```

Until signing is in place, releases are **unsigned**; see the README's
"Distribution safety" section for what to expect from SmartScreen and antivirus
in the meantime.
