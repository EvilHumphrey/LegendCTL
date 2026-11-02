---
title: Installing LegendCTL on Windows (and possible SmartScreen warnings)
description: How to download, check, and run LegendCTL on Windows — including why Windows may show a SmartScreen warning for unsigned or low-reputation downloads, what SHA-256 can and cannot establish, and how to verify each release's build provenance.
---

# Installing LegendCTL on Windows

LegendCTL is a free, open-source, standalone configurator for the ZD Ultimate Legend — no official ZD app required, no drivers, no background service, no telemetry. This page covers downloading it, handling a possible Windows SmartScreen warning, checking downloaded bytes for mismatch or corruption, and separately verifying the build origin of attested releases.

## 1. Download

From the [Releases page](https://github.com/EvilHumphrey/LegendCTL/releases/latest), grab one of:

- **`ZDUltimateLegend-v<version>-windows.zip`** — portable. Unzip anywhere and run `ZD Ultimate Legend.exe`. Nothing is installed; delete the folder to remove it. **Recommended if you just want to try it.**
- **`ZDUltimateLegend-v<version>-Setup.exe`** — installer (adds Start-Menu / Desktop shortcuts; uninstall via Settings → Apps).

### Installer destination policy in the next release

Installer builds from this revision accept only the standard
`%ProgramFiles%\ZDUltimateLegend\` directory. Custom destinations supplied through
`/DIR`, `/LOADINF`, or old installation settings are not supported. The final
destination is checked before files or uninstall registration are changed;
directory links in its existing parent chain are rejected too. This assumes
the normal Program Files permissions have not been weakened by an administrator.
The portable ZIP still supports running from another folder.

If Setup finds an older installation registered in another directory, it blocks
instead of silently leaving two installations sharing one uninstall entry.
Review that installation before migrating: Setup does not move or delete its
files and never launches its old uninstaller. A rejected path uses Inno Setup's
standard folder-name error; `/LOG` records a `LEGENDCTL_INSTALL_PATH_*` or
`LEGENDCTL_PREVIOUS_INSTALL_PATH_REJECTED` reason code. Existing published
installers are unchanged by this source update. The check covers machine-wide
registrations and Setup's current account, not every other user's installations.

## 2. Why Windows may show "Windows protected your PC"

LegendCTL is **not code-signed** and its downloads may have limited reputation. Depending on the file's reputation, how it was downloaded, and system or enterprise policy, Windows SmartScreen may show a blue **"Windows protected your PC"** dialog.

**This warning is a reputation signal, not by itself a malware verdict.** It does not mean that Windows proved the file harmful. You don't have to trust the download blindly, though — you can **verify it yourself** (next section).

If you independently trust the source and accept the risk of running an unsigned
build, Windows may offer this path:

1. Click **More info** in the SmartScreen dialog.
2. Click **Run anyway**.

*(On a managed/work PC, an administrator policy can block "Run anyway" entirely — that's expected on locked-down machines and isn't specific to LegendCTL.)*

**A matching SHA-256 value alone is not a reason to bypass SmartScreen.** The
artifact and `SHA256SUMS.txt` are delivered through the same release channel, so
an attacker who could replace one may be able to replace both.

## 3. Check the downloaded bytes with SHA-256 (recommended)

Checking SHA-256 detects whether your file differs from the value listed in the
release's `SHA256SUMS.txt`. It is useful for catching corruption, incomplete
downloads, or an accidental mismatch. It does **not** authenticate the publisher
when the artifact and checksum came from the same channel.

1. Each release includes a **`SHA256SUMS.txt`** asset listing the official hashes.
2. Open PowerShell in your downloads folder and run the line for whichever file you downloaded:

   ```powershell
   # Portable ZIP (the recommended "just try it" download):
   Get-FileHash .\ZDUltimateLegend-v<version>-windows.zip -Algorithm SHA256
   # Or the installer:
   Get-FileHash .\ZDUltimateLegend-v<version>-Setup.exe -Algorithm SHA256
   ```

3. Compare the printed hash to the matching line in `SHA256SUMS.txt`. If they
   match, your file matches that supplied checksum. If they don't match, delete
   it and re-download.

*(In `SHA256SUMS.txt`, a leading `*` on a filename is standard `sha256sum` notation marking binary-mode hashing — it's not part of the filename.)*

A matching same-channel hash is a byte-comparison check, not publisher
authentication and not the equivalent of an Authenticode signature. For an
authenticated build-origin check on v2.5.0+ releases, use the build-provenance
verification in section 4. (Authenticode code-signing is a separate,
still-planned layer — see the [code-signing policy](code-signing-policy.md).)

## 4. Verify where it was built (build provenance)

From **v2.5.0** onward, release binaries are **built in this repository's public GitHub Actions workflow** ([`release-build.yml`](../.github/workflows/release-build.yml)) — not on a maintainer's machine — and every such release carries a **build-provenance attestation**: a cryptographically signed statement, recorded with GitHub, tying the exact bytes of the release files to the workflow run and source commit that produced them.

Releases **v2.4.1 and earlier were built locally** by the maintainer and are
**not** attested. Their SHA-256 files still support the mismatch/corruption check
in section 3, but those releases do not have this authenticated build-origin path.

**What this proves / doesn't prove — honestly:**

- **Proves with the pinned command below:** the file you downloaded is
  byte-identical to an artifact attested by this repository's
  `release-build.yml` workflow for the named release tag. If someone swapped,
  or modified the file without a matching attestation from that repository,
  workflow, and tag, verification **fails**. Re-uploading identical bytes still
  passes, and this policy does not pin one unique workflow run or expected
  source digest.
- **Does NOT prove:** that the app is code-signed (it isn't yet), that SmartScreen won't warn (it may still warn — see section 2), or that the software is risk-free. Provenance is about *origin*, not a safety rating.

Checking it needs the [GitHub CLI](https://cli.github.com/) (**v2.102.0 or
newer**). That minimum includes the workflow/ref policy flags used below and
security fixes for their matching behavior:

```powershell
gh attestation verify .\ZDUltimateLegend-v<version>-Setup.exe `
  --repo EvilHumphrey/LegendCTL `
  --signer-workflow EvilHumphrey/LegendCTL/.github/workflows/release-build.yml `
  --source-ref refs/tags/v<version>
gh attestation verify .\ZDUltimateLegend-v<version>-windows.zip `
  --repo EvilHumphrey/LegendCTL `
  --signer-workflow EvilHumphrey/LegendCTL/.github/workflows/release-build.yml `
  --source-ref refs/tags/v<version>
```

A good result ends with `✓ Verification succeeded!`. The repository,
signer-workflow, and source-ref constraints are all part of the check; do not
drop them or treat a repository-only verification as equivalent.

**Offline / API-free:** each attested release also ships its attestation as the `attestation-bundle.jsonl` asset, so you can verify without querying GitHub's attestation API. You do need to fetch the trust root **once while online** (`gh attestation trusted-root`) and keep the resulting file — after that, verification runs fully offline:

```powershell
# Run once while online; keep trusted_root.jsonl for later offline use.
# Write BOM-free UTF-8 explicitly so this also works in Windows PowerShell 5.1.
$trustedRootLines = & gh attestation trusted-root
if ($LASTEXITCODE -ne 0 -or -not $trustedRootLines) {
  throw "Could not download the GitHub attestation trusted root."
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText(
  (Join-Path $PWD.Path "trusted_root.jsonl"),
  (($trustedRootLines -join "`n") + "`n"),
  $utf8NoBom
)

# Then verify offline, against the bundle shipped with the release:
gh attestation verify .\ZDUltimateLegend-v<version>-Setup.exe `
  --repo EvilHumphrey/LegendCTL `
  --signer-workflow EvilHumphrey/LegendCTL/.github/workflows/release-build.yml `
  --source-ref refs/tags/v<version> `
  --bundle .\attestation-bundle.jsonl `
  --custom-trusted-root .\trusted_root.jsonl
gh attestation verify .\ZDUltimateLegend-v<version>-windows.zip `
  --repo EvilHumphrey/LegendCTL `
  --signer-workflow EvilHumphrey/LegendCTL/.github/workflows/release-build.yml `
  --source-ref refs/tags/v<version> `
  --bundle .\attestation-bundle.jsonl `
  --custom-trusted-root .\trusted_root.jsonl
```

Offline `--bundle` verification compares the repository and owner names
recorded in the attestation certificate. It cannot detect whether either name
was later renamed or reused. When online verification is available, omitting
`--bundle` lets GitHub CLI fetch through the API and confirm the repository that
currently holds that name.

*(Releases built locally, before this workflow existed, are not attested. Their
SHA-256 files provide the section 3 byte-comparison check, not publisher
authentication.)*

## 5. Why you can trust it beyond the hash

- **Open source.** The full source is on [GitHub](https://github.com/EvilHumphrey/LegendCTL) — read exactly what it does.
- **Built in public CI, with provenance.** Release binaries are produced by a public GitHub Actions workflow and carry a verifiable build attestation (section 4).
- **No telemetry, no network calls.** It talks only to your controller over USB/HID; it doesn't phone home.
- **No drivers, no background service.** It's a plain desktop app; closing it stops it completely.
- **Standalone.** It doesn't require, launch, or modify the official ZD app. (One narrow read-only exception: if the official app's Controller Settings window is already open during an explicit device read, LegendCTL may read a few summary labels from it via Windows UI Automation — it never launches or changes that app.)

## 6. Install via winget

LegendCTL is available from the Windows Package Manager community repository:

```powershell
winget install --id EvilHumphrey.LegendCTL --exact
```

winget fetches and hash-checks the installer for you. The community catalog can
trail the newest GitHub release while an update is validated; check the
currently published version with
`winget show --id EvilHumphrey.LegendCTL --exact`.

## 7. Uninstall

- **Portable ZIP:** delete the folder.
- **Installer:** Settings → Apps → "ZD Ultimate Legend Wrapper" → Uninstall. (User data lives in `%APPDATA%\ZDUltimateLegend\`; delete that folder too if you want a completely clean removal.)
